#!/usr/bin/env python3
"""
Micro TV int. bot v2.0

Should run on enshitifed 10-15 MB RAM
No Discord gateway connection; posts through the REST API with the bot token
(hopefully) Never repeats, as it should remember every video ID it has ever seen per channel, so a
stale/flipping RSS response or a deleted video can't trigger a re-announce
Posts only the newest unseen video per check and older unseen ones are marked
seen
(hopefully) Crash-proof, as every network call has a timeout, every feed is wrapped in
try/except (so dumb I didn't do this earlier), state is written atomically, a corrupt state/config file never
crashes the bot
config.json is re-read every cycle, so it can be edited any time without restart (lets home this doesnt chew the ram
python3 bot.py --once does one check and exits (GitHub Actions needs ts)
"""

import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

BASE = Path(__file__).resolve().parent
CONFIG_PATH = BASE / "config.json"
STATE_PATH = BASE / "state.json"

FEED_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={}"
DISCORD_API = "https://discord.com/api/v10/channels/{}/messages"
ATOM = "{http://www.w3.org/2005/Atom}"
YT = "{http://www.youtube.com/xml/schemas/2015}"
MAX_SEEN = 60  # video ids remembered per channel

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("yt-bot")


#states

def load_env():
    """Tiny .env reader so python-dotenv isn't needed."""
    p = BASE / ".env"
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip("\"'"))


_last_good_config = {"check_interval_minutes": 5, "feeds": []}


def load_config():
    global _last_good_config
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        if isinstance(cfg.get("feeds"), list):
            _last_good_config = cfg
    except Exception as e:
        log.warning("config.json unreadable (%s) - using last good config", e)
    return _last_good_config


def load_state():
    """state = {youtube_channel_id: {"seen": [video ids...]}}
    Old-format entries (plain strings) are dropped, which makes those feeds
    re-initialize silently on the first run."""
    try:
        raw = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            return {
                k: v for k, v in raw.items()
                if isinstance(v, dict) and isinstance(v.get("seen"), list)
            }
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning("state.json unreadable (%s) - keeping a .bad copy, starting fresh", e)
        try:
            os.replace(STATE_PATH, STATE_PATH.with_name("state.json.bad"))
        except OSError:
            pass
    return {}


def save_state(state):
    """Atomic write: a crash mid-save can never leave a half-written file."""
    tmp = STATE_PATH.with_name("state.json.tmp")
    tmp.write_text(json.dumps(state), encoding="utf-8")
    os.replace(tmp, STATE_PATH)


#yt handle

def fetch_entries(channel_id):
    """Return a list of {id, title, published, author} or None on any failure."""
    req = urllib.request.Request(
        FEED_URL.format(channel_id), headers={"User-Agent": "Mozilla/5.0"}
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            root = ET.fromstring(r.read(1_000_000))
    except Exception as e:
        log.warning("Feed error for %s: %s", channel_id, e)
        return None

    entries = []
    for e in root.findall(ATOM + "entry"):
        vid = e.findtext(YT + "videoId")
        if not vid:
            continue
        entries.append({
            "id": vid,
            "title": e.findtext(ATOM + "title") or "New video",
            "published": e.findtext(ATOM + "published") or "",
            "author": e.findtext(f"{ATOM}author/{ATOM}name") or "",
        })
    return entries


#discord shitty rest api

def post_to_discord(token, feed, entry):
    vid = entry["id"]
    url = f"https://www.youtube.com/watch?v={vid}"
    role_id = feed.get("role_id")
    mention = f"<@&{role_id}> " if role_id else ""

    payload = {
        "content": f"{mention}**{feed['name']}** has made a broadcast\n{url}",
        "embeds": [{
            "title": entry["title"][:256],
            "url": url,
            "description": f"New upload from **{entry['author'] or feed['name']}**",
            "color": 0xFF0000,
            "image": {"url": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"},
            "footer": {"text": "YouTube"},
        }],
        "allowed_mentions": {
            "parse": [],
            "roles": [str(role_id)] if role_id else [],
        },
    }
    req = urllib.request.Request(
        DISCORD_API.format(feed["discord_channel_id"]),
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
            # Discord's Cloudflare blocks urllib's default User-Agent
            "User-Agent": "DiscordBot (yt-notifier, 2.0)",
        },
    )

    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=15):
                return True
        except urllib.error.HTTPError as e:
            if e.code == 429:  # rate limited: wait as told, then retry
                try:
                    wait = float(json.loads(e.read()).get("retry_after", 5))
                except Exception:
                    wait = 5.0
                time.sleep(min(wait, 60) + 0.5)
            elif e.code >= 500:
                time.sleep(5)
            else:  # 400/401/403/404 - retrying won't help
                log.error("Discord rejected post for '%s' (HTTP %s): %s",
                          feed["name"], e.code, e.read()[:200])
                return False
        except Exception as e:
            log.warning("Discord post error (attempt %d): %s", attempt + 1, e)
            time.sleep(5)
    return False


#the shit

def check_feed(feed, state, token):
    cid = feed.get("youtube_channel_id", "")
    if not cid or cid.startswith("REPLACE"):
        return

    entries = fetch_entries(cid)
    if not entries:
        return  #if empty do not do shit

    ids = [e["id"] for e in entries]
    st = state.get(cid)

    # First time it sees this channel remember what's there, but dont yap
    if st is None:
        state[cid] = {"seen": ids[:MAX_SEEN]}
        save_state(state)
        log.info("Initialized '%s' with %d existing videos (nothing posted)", feed["name"], len(ids))
        return

    seen = set(st["seen"])
    unseen = [e for e in entries if e["id"] not in seen]
    if not unseen:
        return

    newest = max(unseen, key=lambda e: e["published"])

    # Mark EVERYTHING in the feed as seen and save BEFORE posting.
    # This dumb line is so stupid yet so smart
    st["seen"] = (st["seen"] + [i for i in ids if i not in seen])[-MAX_SEEN:]
    save_state(state)

    if post_to_discord(token, feed, newest):
        log.info("Announced %s: %s", feed["name"], newest["title"])
    else:
        log.error("Could not announce %s: %s (marked as seen, will not retry)",
                  feed["name"], newest["title"])


def main():
    load_env()
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        sys.exit("DISCORD_TOKEN is not set (put it in .env or the environment).")

    once = "--once" in sys.argv  #shit for github
    state = load_state()
    log.info("Started%s.", " (single run)" if once else "")

    while True:
        interval = 5
        try:
            cfg = load_config()
            interval = max(1, int(cfg.get("check_interval_minutes", 5)))
            for feed in cfg["feeds"]:
                try:
                    check_feed(feed, state, token)
                except Exception:
                    log.exception("Error checking feed '%s'", feed.get("name"))
                time.sleep(1)
        except Exception:
            log.exception("Cycle failed")
        if once:
            return
        time.sleep(interval * 60)


if __name__ == "__main__": #you better run like a good boy!
    main()

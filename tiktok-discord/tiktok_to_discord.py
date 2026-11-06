#!/usr/bin/env python3
"""
TikTok -> Discord announcer.

Checks TikTok accounts for new videos and posts a custom message to a Discord webhook.

Usage:
    python tiktok_to_discord.py          # runs forever, checking every N seconds
    python tiktok_to_discord.py --once   # checks once and exits (good for cron)
    python tiktok_to_discord.py --setup  # one-time: applies the name + avatar to the webhook
"""

import base64
import json
import os
import sys
import time

import requests
from yt_dlp import YoutubeDL

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, "config.json")
STATE_PATH = os.path.join(BASE, "seen.json")


class SafeDict(dict):
    """Leaves unknown {placeholders} untouched instead of crashing."""

    def __missing__(self, key):
        return "{" + key + "}"


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def fetch_latest(username, limit=6):
    """Return the account's latest videos (newest first)."""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "skip_download": True,
        "playlistend": limit,
    }
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"https://www.tiktok.com/@{username}", download=False)

    videos = []
    for e in info.get("entries") or []:
        vid = str(e.get("id"))
        thumbs = e.get("thumbnails") or []
        videos.append(
            {
                "id": vid,
                "url": e.get("url") or f"https://www.tiktok.com/@{username}/video/{vid}",
                "title": (e.get("title") or e.get("description") or "").strip(),
                "thumbnail": thumbs[-1]["url"] if thumbs else None,
            }
        )
    return videos


def post_to_discord(cfg, account, video):
    link = video["url"]
    if cfg.get("embed_link_domain"):
        link = link.replace("tiktok.com", cfg["embed_link_domain"], 1)
    values = SafeDict(
        username=account["username"],
        title=video["title"],
        description=video["title"],
        url=link,
        id=video["id"],
    )
    template = account.get("message") or cfg.get(
        "default_message", "🎵 **{username}** just posted on TikTok!\n{url}"
    )
    content = template.format_map(values)

    payload = {"content": content}
    if cfg.get("bot_name"):
        payload["username"] = cfg["bot_name"]
    if cfg.get("bot_avatar_url"):
        payload["avatar_url"] = cfg["bot_avatar_url"]

    embed_cfg = cfg.get("embed", {})
    if embed_cfg.get("enabled"):
        embed = {
            "title": embed_cfg.get("title", "{username} posted on TikTok").format_map(values),
            "description": embed_cfg.get("description", "{title}").format_map(values)[:4000],
            "url": video["url"],
            "color": int(str(embed_cfg.get("color", "FE2C55")).lstrip("#"), 16),
        }
        if video.get("thumbnail"):
            embed["image"] = {"url": video["thumbnail"]}
        payload["embeds"] = [embed]

    webhook = account.get("webhook_url") or cfg["webhook_url"]
    for _ in range(3):
        r = requests.post(webhook, json=payload, timeout=20)
        if r.status_code == 429:  # rate limited
            time.sleep(float(r.json().get("retry_after", 2)) + 0.5)
            continue
        r.raise_for_status()
        return
    print("  ! Gave up after repeated rate limits")


def check_all(cfg, state):
    for account in cfg["accounts"]:
        name = account["username"].lstrip("@")
        account["username"] = name
        try:
            videos = fetch_latest(name)
        except Exception as exc:  # network / TikTok changes / etc.
            print(f"[{name}] fetch failed: {exc}")
            continue

        seen = state.get(name)
        ids = [v["id"] for v in videos]

        # First run for this account: remember what's there, don't spam.
        if seen is None:
            state[name] = ids
            print(f"[{name}] first run, remembered {len(ids)} existing videos")
            continue

        new = [v for v in videos if v["id"] not in seen]
        for video in reversed(new):  # oldest first
            print(f"[{name}] new video {video['id']}")
            try:
                post_to_discord(cfg, account, video)
                seen.append(video["id"])
            except Exception as exc:
                print(f"  ! Discord post failed: {exc}")
        state[name] = seen[-100:]
        save_json(STATE_PATH, state)

    save_json(STATE_PATH, state)


def setup_webhook(cfg):
    """One-time: sets the webhook's name and profile picture in Discord."""
    avatar_path = os.path.join(BASE, cfg.get("avatar_file", "avatar.png"))
    with open(avatar_path, "rb") as f:
        data_uri = "data:image/png;base64," + base64.b64encode(f.read()).decode()
    body = {"name": cfg.get("bot_name", "TikTok Alerts"), "avatar": data_uri}
    r = requests.patch(cfg["webhook_url"], json=body, timeout=30)
    r.raise_for_status()
    print(f"Webhook updated: name = {body['name']!r}, avatar = {avatar_path}")


def main():
    cfg = load_json(CONFIG_PATH, None)
    if not cfg:
        sys.exit("config.json not found next to the script.")
    if os.environ.get("DISCORD_WEBHOOK_URL"):
        cfg["webhook_url"] = os.environ["DISCORD_WEBHOOK_URL"].strip()
    if "PASTE" in cfg.get("webhook_url", "PASTE"):
        sys.exit("Put your Discord webhook URL in config.json first.")

    if "--setup" in sys.argv:
        setup_webhook(cfg)
        return

    state = load_json(STATE_PATH, {})
    once = "--once" in sys.argv
    interval = max(int(cfg.get("check_interval_seconds", 300)), 60)

    while True:
        check_all(cfg, state)
        if once:
            break
        time.sleep(interval)


if __name__ == "__main__":
    main()

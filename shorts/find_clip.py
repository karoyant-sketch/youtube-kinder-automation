"""Find the best recent GTA VI Twitch clip via the official Twitch Helix API.

Rules applied:
- only English clips, 12-60 seconds long, from the last 7 days
- skip clips that were already posted (shorts/posted.json)
- prefer streamers whose channel description allows clip usage;
  every candidate is marked with `permission` so nothing unclear gets published
"""
import datetime as dt
import json
import os
import pathlib
import re
import sys

import requests

CLIENT_ID = os.environ["TWITCH_CLIENT_ID"]
CLIENT_SECRET = os.environ["TWITCH_CLIENT_SECRET"]
GAME_NAME = os.environ.get("GAME_NAME", "Grand Theft Auto VI")
ROOT = pathlib.Path(__file__).parent
POSTED = ROOT / "posted.json"
OUT = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "work/clip.json")

PERMISSION_PATTERNS = [
    r"feel free to (use|clip|repost|share)",
    r"(use|repost|reupload|share) my clips",
    r"clips? (are )?(free|ok|okay|allowed) to (use|post|repost)",
    r"clipping (is )?(allowed|welcome|encouraged)",
    r"clippers? (are )?welcome",
    r"you can (use|post|repost) my (clips|content)",
]


def token() -> str:
    r = requests.post(
        "https://id.twitch.tv/oauth2/token",
        data={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
              "grant_type": "client_credentials"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def main() -> None:
    h = {"Client-Id": CLIENT_ID, "Authorization": f"Bearer {token()}"}
    api = "https://api.twitch.tv/helix"

    games = requests.get(f"{api}/games", params={"name": GAME_NAME}, headers=h, timeout=30).json()["data"]
    if not games:
        sys.exit(f"Game not found on Twitch: {GAME_NAME}")
    game_id = games[0]["id"]

    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    clips = requests.get(f"{api}/clips", params={"game_id": game_id, "started_at": since, "first": 100},
                         headers=h, timeout=30).json()["data"]

    posted = set(json.loads(POSTED.read_text())) if POSTED.exists() else set()
    clips = [c for c in clips
             if c["language"].startswith("en") and 12 <= c["duration"] <= 60 and c["id"] not in posted]
    if not clips:
        sys.exit("No matching clips found")

    ids = list({c["broadcaster_id"] for c in clips})[:100]
    users = requests.get(f"{api}/users", params=[("id", i) for i in ids], headers=h, timeout=30).json()["data"]
    desc = {u["id"]: u["description"] for u in users}

    for c in clips:
        text = desc.get(c["broadcaster_id"], "").lower()
        c["permission"] = any(re.search(p, text) for p in PERMISSION_PATTERNS)
        c["broadcaster_description"] = desc.get(c["broadcaster_id"], "")

    clips.sort(key=lambda c: (c["permission"], c["view_count"]), reverse=True)
    best = clips[0]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"best": best, "candidates": clips[:10]}, indent=2))
    print(f"Picked: {best['broadcaster_name']} | {best['title']} | {best['view_count']} views | "
          f"permission={best['permission']} | {best['url']}")


if __name__ == "__main__":
    main()

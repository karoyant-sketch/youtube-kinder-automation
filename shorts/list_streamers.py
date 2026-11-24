"""List English GTA VI streamers (live now) with contact info from their Twitch bio.

Output: output/streamers.json  (mid-size streamers first: they answer permission requests more often)
"""
import json
import os
import pathlib
import re

import requests

CLIENT_ID = os.environ["TWITCH_CLIENT_ID"]
tok = requests.post("https://id.twitch.tv/oauth2/token", data={
    "client_id": CLIENT_ID, "client_secret": os.environ["TWITCH_CLIENT_SECRET"],
    "grant_type": "client_credentials"}, timeout=30).json()["access_token"]
h = {"Client-Id": CLIENT_ID, "Authorization": f"Bearer {tok}"}
api = "https://api.twitch.tv/helix"

game_id = requests.get(f"{api}/games", params={"name": "Grand Theft Auto VI"}, headers=h, timeout=30).json()["data"][0]["id"]

streams, cursor = [], None
for _ in range(5):
    p = {"game_id": game_id, "language": "en", "first": 100}
    if cursor:
        p["after"] = cursor
    r = requests.get(f"{api}/streams", params=p, headers=h, timeout=30).json()
    streams += r["data"]
    cursor = r.get("pagination", {}).get("cursor")
    if not cursor:
        break

out = []
for i in range(0, len(streams), 100):
    batch = streams[i:i + 100]
    users = requests.get(f"{api}/users", params=[("id", s["user_id"]) for s in batch],
                         headers=h, timeout=30).json()["data"]
    desc = {u["id"]: u["description"] for u in users}
    for s in batch:
        d = desc.get(s["user_id"], "")
        out.append({
            "name": s["user_name"], "login": s["user_login"], "viewers": s["viewer_count"],
            "title": s["title"], "emails": re.findall(r"[\w.+-]+@[\w-]+\.[\w.]+", d), "bio": d,
            "url": f"https://twitch.tv/{s['user_login']}",
        })

out.sort(key=lambda s: (not (50 <= s["viewers"] <= 3000), not s["emails"], -s["viewers"]))
pathlib.Path("output").mkdir(exist_ok=True)
pathlib.Path("output/streamers.json").write_text(json.dumps(out, indent=2))
print(len(out), "streamers;", sum(1 for s in out if s["emails"]), "with email")

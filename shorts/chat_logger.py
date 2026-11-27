"""Record the LIVE Twitch chat of our allowed streamers (official read-only IRC, anonymous login).

Runs inside the "GTA6 Chat Logger" workflow for up to MAX_MINUTES, then the workflow restarts itself.
While an enabled Twitch source is live, every chat message is stored as {"t": unix_time, "m": text}
(no user names) in chat/<login>/<stream_id>.jsonl. fetch_candidates.py later finds the moments where
chat exploded (LUL/KEKW/LMAO/💀...) and cuts those moments out of the saved VOD.
"""
import json
import os
import pathlib
import random
import socket
import subprocess
import time

import requests

ROOT = pathlib.Path(__file__).resolve().parent.parent
CFG = json.loads((ROOT / "config/sources.json").read_text())
LOGINS = [s["login"].lower() for s in CFG["sources"] if s.get("enabled") and s["platform"] == "twitch"]
MAX_SECONDS = int(os.environ.get("MAX_MINUTES", "330")) * 60
START = time.time()

cid = os.environ["TWITCH_CLIENT_ID"]
tok = requests.post("https://id.twitch.tv/oauth2/token", data={
    "client_id": cid, "client_secret": os.environ["TWITCH_CLIENT_SECRET"],
    "grant_type": "client_credentials"}, timeout=30).json()["access_token"]
H = {"Client-Id": cid, "Authorization": f"Bearer {tok}"}


def live_streams():
    if not LOGINS:
        return {}
    r = requests.get("https://api.twitch.tv/helix/streams", params=[("user_login", l) for l in LOGINS],
                     headers=H, timeout=30).json().get("data", [])
    return {s["user_login"].lower(): s["id"] for s in r}


def save():
    """Commit + push collected chat so far (safe to call repeatedly)."""
    subprocess.run("git add chat && (git diff --cached --quiet || (git commit -q -m 'Chat log [skip ci]' && "
                   "git pull -q --rebase && git push -q))", shell=True, cwd=ROOT)


def log_chat(streams):
    """Stay connected while at least one source is live; return when all are offline or time is up."""
    sock = socket.create_connection(("irc.chat.twitch.tv", 6667), timeout=30)
    sock.sendall(f"NICK justinfan{random.randint(10000, 99999)}\r\n".encode())
    for login in streams:
        sock.sendall(f"JOIN #{login}\r\n".encode())
    files = {}
    for login, sid in streams.items():
        p = ROOT / "chat" / login / f"{sid}.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        files[login] = p.open("a", encoding="utf-8")
    buf, last_check, last_save = "", time.time(), time.time()
    sock.settimeout(5)
    while time.time() - START < MAX_SECONDS:
        try:
            data = sock.recv(65536).decode("utf-8", "ignore")
            if not data:
                break
            buf += data
        except socket.timeout:
            pass
        *lines, buf = buf.split("\r\n")
        for line in lines:
            if line.startswith("PING"):
                sock.sendall(line.replace("PING", "PONG", 1).encode() + b"\r\n")
            elif " PRIVMSG #" in line:
                chan = line.split(" PRIVMSG #", 1)[1].split(" ", 1)[0].lower()
                text = line.split(" :", 2)[-1] if line.startswith(":") else line.split(" :", 1)[-1]
                if chan in files:
                    files[chan].write(json.dumps({"t": round(time.time(), 1), "m": text[:200]}) + "\n")
        if time.time() - last_check > 180:
            last_check = time.time()
            now = live_streams()
            if not any(l in now for l in streams):
                break
        if time.time() - last_save > 1800:
            last_save = time.time()
            for f in files.values():
                f.flush()
            save()
    for f in files.values():
        f.close()
    sock.close()


while time.time() - START < MAX_SECONDS:
    live = live_streams()
    if live:
        print("live:", live)
        log_chat(live)
        save()
    else:
        time.sleep(120)
save()

"""Daily GTA 6 Shorts pipeline (runs in GitHub Actions, no PC needed).

1. pick an ALLOWED source (config/sources.json, enabled only) and its best new clip
   (Twitch Helix API or Kick), skipping anything already posted (state/posted.json)
2. Claude checks the clip (thumbnail + transcript): advertiser-friendly? real highlight?
   and writes hook, YouTube title, description, tags and (gameplay mode) narration
3. gameplay mode: ElevenLabs narration, streamer voice/face removed
   campaign mode: facecam + original audio kept, captions, required hashtags/credit
4. upload to Postey and create a YouTube DRAFT on the configured account
5. Telegram: tell the owner what is needed (approve the draft, submit to campaign)

Secrets: TWITCH_CLIENT_ID/SECRET, ANTHROPIC_API_KEY, ELEVENLABS_API_KEY, POSTEY_MCP_KEY,
         TELEGRAM_BOT_TOKEN (+ TELEGRAM_CHAT_ID)
"""
import base64
import datetime as dt
import json
import os
import pathlib
import re
import subprocess
import sys

import requests

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from notify import notify  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
CFG = json.loads((ROOT / "config/sources.json").read_text())
POSTED_F = ROOT / "state/posted.json"
POSTED = json.loads(POSTED_F.read_text()) if POSTED_F.exists() else []
WORK = ROOT / "work"
WORK.mkdir(exist_ok=True)
VOICE_ID = "xB7ZTAdAjd7cI20IXiAL"  # ElevenLabs "Chris Beck - Rocket Radio"
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5")
NSFW = re.compile(r"strip|gyat|horny|thirst|booty|twerk|lap ?dance|nsfw|nude|naked|sex|boob|hooker|onlyfans|shower", re.I)


def missing(*names):
    return [n for n in names if not os.environ.get(n)]


def fail(msg):
    notify("⚠️ Open World Chaos: " + msg)
    sys.exit(1)


# ---------------------------------------------------------------- clip sources
def twitch_clips(login):
    cid = os.environ["TWITCH_CLIENT_ID"]
    tok = requests.post("https://id.twitch.tv/oauth2/token", data={
        "client_id": cid, "client_secret": os.environ["TWITCH_CLIENT_SECRET"],
        "grant_type": "client_credentials"}, timeout=30).json()["access_token"]
    h = {"Client-Id": cid, "Authorization": f"Bearer {tok}"}
    api = "https://api.twitch.tv/helix"
    uid = requests.get(f"{api}/users", params={"login": login}, headers=h, timeout=30).json()["data"][0]["id"]
    game = requests.get(f"{api}/games", params={"name": "Grand Theft Auto VI"}, headers=h, timeout=30).json()["data"][0]["id"]
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    data = requests.get(f"{api}/clips", params={"broadcaster_id": uid, "started_at": since, "first": 50},
                        headers=h, timeout=30).json()["data"]
    return [{"id": c["id"], "url": c["url"], "title": c["title"], "views": c["view_count"],
             "duration": c["duration"], "thumb": c["thumbnail_url"], "created": c["created_at"],
             "streamer": c["broadcaster_name"]} for c in data if c["game_id"] == game]


def kick_clips(login):
    r = requests.get(f"https://kick.com/api/v2/channels/{login}/clips", params={"sort": "view", "time": "week"},
                     headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"}, timeout=30)
    if not r.ok:
        raise RuntimeError(f"Kick clips not reachable ({r.status_code})")
    out = []
    for c in r.json().get("clips", []):
        cat = (c.get("category") or {}).get("name", "")
        if "Grand Theft Auto VI" not in cat and "GTA" not in cat:
            continue
        out.append({"id": c["id"], "url": f"https://kick.com/{login}/clips/{c['id']}", "title": c.get("title", ""),
                    "views": c.get("views") or c.get("view_count", 0), "duration": c.get("duration", 30),
                    "thumb": c.get("thumbnail_url"), "created": c.get("created_at", ""), "streamer": login})
    return out


def pick_clip():
    sources = [s for s in CFG["sources"] if s.get("enabled")]
    if not sources:
        fail("Keine freigegebene Quelle aktiv. Tritt einer Kampagne bei (z. B. dasgasd3m auf Content Rewards) "
             "und sag mir im Claude-Chat Bescheid, dann schalte ich sie frei.")
    cands = []
    for s in sources:
        try:
            clips = twitch_clips(s["login"]) if s["platform"] == "twitch" else kick_clips(s["login"])
        except Exception as e:  # noqa: BLE001
            print(f"{s['name']}: {e}")
            continue
        for c in clips:
            if c["id"] in POSTED or NSFW.search(c["title"]) or not 8 <= float(c["duration"]) <= 60:
                continue
            if s.get("since") and c["created"][:10] < s["since"]:
                continue
            cands.append((s, c))
    cands.sort(key=lambda sc: sc[1]["views"], reverse=True)
    return cands[:5]


# ---------------------------------------------------------------- AI helpers
def transcript(path):
    from faster_whisper import WhisperModel
    import numpy as np
    pcm = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", str(path), "-f", "s16le", "-ac", "1", "-ar", "16000", "-"],
                         capture_output=True, check=True).stdout
    a = np.frombuffer(pcm, np.int16).astype(np.float32) / 32768
    try:
        return " ".join(s.text for s in WhisperModel("base", device="cpu", compute_type="int8").transcribe(a, language="en")[0])
    except (IndexError, ValueError):
        return ""


def frames_b64(path, n=4):
    dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                               capture_output=True, text=True).stdout or 20)
    imgs = []
    for i in range(n):
        f = WORK / f"f{i}.jpg"
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-ss", str(dur * (i + 0.5) / n), "-i", str(path),
                        "-frames:v", "1", "-vf", "scale=640:-1", str(f)], check=True)
        imgs.append(base64.b64encode(f.read_bytes()).decode())
    return imgs, dur


def claude_review(src, clip, text, imgs, dur):
    mode = src["mode"]
    prompt = f"""You review a GTA 6 Twitch/Kick clip for an English YouTube Shorts channel "Open World Chaos".
Clip title: {clip['title']!r} by {clip['streamer']} ({clip['views']} views, {dur:.0f}s).
Streamer speech transcript: {text[:1500]!r}
Four frames from the clip are attached in order.

Mode: {mode}. {"Gameplay only; the streamer's voice is removed and we add our own narration." if mode == "gameplay" else "Campaign rules: keep the streamer's face and original audio, never change what he meant."}

Return ONLY JSON:
{{"ok": true/false,            // false if sexual/nudity/strip-club, gore, slurs, or boring/no clear highlight
  "reason": "short reason",
  "hook": "on-screen hook, max 38 chars, all caps, truthful",
  "youtube_title": "max 90 chars incl. #GTA6 #Shorts, truthful",
  "description": "2-3 short lines",
  "tags": "comma separated, max 10",
  "narration": {"null" if mode != "gameplay" else '"energetic US-English narration, 40-60 words, may use [laughs] [excited] tags, matching the visible action, never invent events you cannot see"'},
  "pan": "optional camera pan keys like 0:0.5,8:0.8 (time s : horizontal center 0-1) or empty"}}"""
    content = [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": i}} for i in imgs]
    content.append({"type": "text", "text": prompt})
    r = requests.post("https://api.anthropic.com/v1/messages", headers={
        "x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01", "content-type": "application/json"},
        json={"model": CLAUDE_MODEL, "max_tokens": 1200, "messages": [{"role": "user", "content": content}]}, timeout=120)
    r.raise_for_status()
    raw = "".join(b.get("text", "") for b in r.json()["content"])
    return json.loads(raw[raw.find("{"): raw.rfind("}") + 1])


def elevenlabs(text, out):
    r = requests.post(f"https://api.elevenlabs.io/v1/text-to-speech/{VOICE_ID}",
                      headers={"xi-api-key": os.environ["ELEVENLABS_API_KEY"]},
                      json={"text": text, "model_id": "eleven_v3"}, timeout=180)
    r.raise_for_status()
    out.write_bytes(r.content)


# ---------------------------------------------------------------- Postey (MCP over HTTP)
class Postey:
    URL = "https://mcp.postey.ai/mcp"

    def __init__(self, key):
        self.h = {"X-API-Key": key, "Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        self.n = 0
        r = self._rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                     "clientInfo": {"name": "owc-pipeline", "version": "1"}})
        if r.headers.get("Mcp-Session-Id"):
            self.h["Mcp-Session-Id"] = r.headers["Mcp-Session-Id"]
        requests.post(self.URL, headers=self.h, json={"jsonrpc": "2.0", "method": "notifications/initialized"}, timeout=30)

    def _rpc(self, method, params):
        self.n += 1
        r = requests.post(self.URL, headers=self.h, json={"jsonrpc": "2.0", "id": self.n, "method": method,
                                                          "params": params}, timeout=300)
        r.raise_for_status()
        return r

    def call(self, tool, args):
        r = self._rpc("tools/call", {"name": tool, "arguments": args})
        body = r.text
        if "text/event-stream" in r.headers.get("Content-Type", ""):
            body = [ln[5:] for ln in body.splitlines() if ln.startswith("data:")][-1]
        res = json.loads(body)
        if "error" in res:
            raise RuntimeError(res["error"])
        txt = "".join(c.get("text", "") for c in res["result"].get("content", []))
        data = json.loads(txt) if txt.strip().startswith("{") else {"raw": txt}
        if res["result"].get("isError") or data.get("success") is False:
            raise RuntimeError(txt[:400])
        return data.get("data", data)


# ---------------------------------------------------------------- main
def main():
    need = missing("TWITCH_CLIENT_ID", "TWITCH_CLIENT_SECRET", "ANTHROPIC_API_KEY", "POSTEY_MCP_KEY")
    if need:
        fail("Es fehlen GitHub-Secrets: " + ", ".join(need) + ". Trag sie hier ein: "
             "https://github.com/karoyant-sketch/youtube-kinder-automation/settings/secrets/actions/new")

    cands = pick_clip()
    if not cands:
        fail("Heute kein neuer, passender Clip bei den freigegebenen Streamern gefunden. Ich versuche es beim nächsten Lauf erneut.")

    for src, clip in cands:
        raw = WORK / "raw.mp4"
        raw.unlink(missing_ok=True)
        subprocess.run(["yt-dlp", "-q", "-f", "best[ext=mp4]/best", "-o", str(raw), clip["url"]], check=True)
        imgs, dur = frames_b64(raw)
        review = claude_review(src, clip, transcript(raw), imgs, dur)
        print(clip["id"], review.get("ok"), review.get("reason"))
        if review.get("ok"):
            break
        POSTED.append(clip["id"])  # never look at a rejected clip again
    else:
        POSTED_F.write_text(json.dumps(POSTED, indent=1))
        fail("Alle Kandidaten von heute waren ungeeignet (anzüglich oder kein Highlight). Nächster Versuch beim nächsten Lauf.")

    meta = WORK / "clip.json"
    meta.write_text(json.dumps({"best": {"title": clip["title"], "duration": min(dur, 59),
                                         "broadcaster_name": clip["streamer"], "credit": src.get("credit")}}))
    env = dict(os.environ)
    narr = ""
    if src["mode"] == "gameplay":
        if missing("ELEVENLABS_API_KEY"):
            fail("Für den Kommentar fehlt das GitHub-Secret ELEVENLABS_API_KEY.")
        narr = str(WORK / "narr.mp3")
        elevenlabs(review["narration"], pathlib.Path(narr))
        if review.get("pan"):
            env["PAN"] = review["pan"]
    else:
        env["LAYOUT"] = "campaign"
    out = WORK / "short.mp4"
    subprocess.run([sys.executable, str(ROOT / "shorts/make_short.py"), str(raw), str(meta), str(out), narr,
                    review["hook"]], check=True, env=env)

    # ---- Postey draft
    desc = review.get("description", "")
    if src.get("credit"):
        desc += "\n\n" + src["credit"]
    if src.get("required_hashtags"):
        desc += "\n" + " ".join(src["required_hashtags"])
    title = review["youtube_title"][:100]
    p = Postey(os.environ["POSTEY_MCP_KEY"])
    up = p.call("upload_media", {"request": {"platform": "YOUTUBE", "source_type": "base64", "filename": "short.mp4",
                                             "file_base64": base64.b64encode(out.read_bytes()).decode()}})
    cdn = up.get("cdn_url") or (up.get("cdn_urls") or [None])[0]
    post = p.call("create_post", {"request": {
        "account_id": CFG["postey_account_id"], "platform": "YOUTUBE", "title": f"OWC {clip['streamer']} {clip['id']}"[:50],
        "idempotency_key": f"owc-{clip['id']}",
        "contents": [{"youtube_title": title, "text": desc, "media_urls": [cdn], "youtube_tags": review.get("tags", ""),
                      "youtube_category_id": "20", "youtube_made_for_kids": False}]}})
    post_id = post.get("post_id") or post.get("id")

    POSTED.append(clip["id"])
    POSTED_F.write_text(json.dumps(POSTED, indent=1))
    msg = (f"🎬 Neues Short bereit (Entwurf in Postey, Post {post_id}):\n{title}\nClip: {clip['url']}\n\n"
           f"👉 Bitte in der Postey-App prüfen und veröffentlichen.")
    if src["mode"] == "campaign" and src.get("submit_url"):
        msg += (f"\n💰 Danach den YouTube-Link INNERHALB VON 30 MIN hier einreichen: {src['submit_url']}")
    notify(msg)


if __name__ == "__main__":
    main()

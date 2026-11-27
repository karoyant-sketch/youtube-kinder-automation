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


# ---------------------------------------------------------------- chat-explosion highlights from VODs
LAUGH = re.compile(r"LUL|KEKW|OMEGALUL|ICANT|LMA+O|LMFAO|\bLO+L\b|HA(HA)+|\bxD\b|Pepega|\U0001F480|\U0001F602|\U0001F923|"
                   r"\bCLIP\b|clip (it|that)|\bDEAD\b|NAHH+|\bBRO+\b", re.I)


def chat_peaks(msgs, vod_start, vod_len, top=3, before=30, after=8):
    """msgs: [{"t": unix, "m": text}] -> [(start_s, end_s, score, sample)] in VOD seconds.

    Score per 5 s bucket = messages + 3 x laugh-messages, compared with the average of the 2 minutes before
    (so a busy chat alone does not count, only a sudden explosion). Chat lags the stream by ~5-10 s, so the
    clip starts `before` seconds before the peak and ends `after` seconds after it."""
    B = 5
    n = int(vod_len // B) + 1
    score, texts = [0.0] * n, [[] for _ in range(n)]
    for m in msgs:
        i = int((m["t"] - vod_start) // B)
        if 0 <= i < n:
            lau = bool(LAUGH.search(m["m"]))
            score[i] += 1 + 3 * lau
            if lau:
                texts[i].append(m["m"])
    win = [sum(score[i:i + 3]) for i in range(n)]  # 15 s window
    spikes = []
    for i in range(24, n):
        base = sum(score[i - 24:i]) / 24 * 3 + 6
        laughs = sum(len(t) for t in texts[i:i + 3])
        if laughs >= 4:
            spikes.append((win[i] / base * (1 + laughs / 10), i, laughs))
    spikes.sort(reverse=True)
    out = []
    for sc, i, laughs in spikes:
        if sc < 3:  # not a real explosion
            break
        peak = i * B + 10
        if any(abs(peak - (o[0] + before)) < 120 for o in out):
            continue
        s0 = max(0, peak - before)
        sample = [t for b in texts[i:i + 3] for t in b][:12]
        out.append((s0, min(vod_len, peak + after), round(sc, 2), sample))
        if len(out) >= top:
            break
    return out


def vod_candidates(max_vods=3):
    """Chat-explosion moments from saved VODs of enabled Twitch sources that have a chat log."""
    cid = os.environ["TWITCH_CLIENT_ID"]
    tok = requests.post("https://id.twitch.tv/oauth2/token", data={
        "client_id": cid, "client_secret": os.environ["TWITCH_CLIENT_SECRET"],
        "grant_type": "client_credentials"}, timeout=30).json()["access_token"]
    h = {"Client-Id": cid, "Authorization": f"Bearer {tok}"}
    api = "https://api.twitch.tv/helix"
    cands = []
    for s in CFG["sources"]:
        if not (s.get("enabled") and s["platform"] == "twitch"):
            continue
        logs = ROOT / "chat" / s["login"].lower()
        if not logs.exists():
            print(f"{s['name']}: no chat log yet")
            continue
        uid = requests.get(f"{api}/users", params={"login": s["login"]}, headers=h, timeout=30).json()["data"][0]["id"]
        vods = requests.get(f"{api}/videos", params={"user_id": uid, "type": "archive", "first": 10},
                            headers=h, timeout=30).json()["data"]
        used = 0
        for v in vods:
            f = logs / f"{v.get('stream_id')}.jsonl"
            if not f.exists():
                continue
            msgs = [json.loads(l) for l in f.read_text(encoding="utf-8").splitlines() if l.strip()]
            start = dt.datetime.fromisoformat(v["created_at"].replace("Z", "+00:00")).timestamp()
            dur = v["duration"]  # like 3h1m59s
            secs = sum(int(x) * {"h": 3600, "m": 60, "s": 1}[u] for x, u in re.findall(r"(\d+)([hms])", dur))
            for s0, e0, sc, sample in chat_peaks(msgs, start, secs):
                cid_ = f"v{v['id']}_{int(s0)}"
                if cid_ in POSTED or _used_near(v["id"], s0):
                    continue
                cands.append((s, {"id": cid_, "url": f"https://www.twitch.tv/videos/{v['id']}", "vod_start": s0,
                                  "vod_end": e0, "title": v["title"], "views": sc, "chat_score": sc,
                                  "chat_sample": sample, "duration": e0 - s0, "created": v["created_at"],
                                  "streamer": v["user_name"]}))
            used += 1
            if used >= max_vods:
                break
    cands.sort(key=lambda sc: sc[1]["chat_score"], reverse=True)
    return cands[:5]


def _used_near(vod_id, start, gap=150):
    """True if we already posted a moment of this VOD within `gap` seconds."""
    for pid in POSTED:
        m = re.fullmatch(r"v(\d+)_(\d+)", pid)
        if m and m.group(1) == str(vod_id) and abs(int(m.group(2)) - start) < gap:
            return True
    return False


def audio_candidates(max_vods=2, per_vod=4, before=22, after=10):
    """Old streams without chat log: find the loudest sudden reactions (screams/laughs) in the VOD audio."""
    import numpy as np
    cid = os.environ["TWITCH_CLIENT_ID"]
    tok = requests.post("https://id.twitch.tv/oauth2/token", data={
        "client_id": cid, "client_secret": os.environ["TWITCH_CLIENT_SECRET"],
        "grant_type": "client_credentials"}, timeout=30).json()["access_token"]
    h = {"Client-Id": cid, "Authorization": f"Bearer {tok}"}
    api = "https://api.twitch.tv/helix"
    cands = []
    for s in CFG["sources"]:
        if not (s.get("enabled") and s["platform"] == "twitch"):
            continue
        uid = requests.get(f"{api}/users", params={"login": s["login"]}, headers=h, timeout=30).json()["data"][0]["id"]
        vods = requests.get(f"{api}/videos", params={"user_id": uid, "type": "archive", "first": 10},
                            headers=h, timeout=30).json()["data"]
        for v in vods[:max_vods]:
            if s.get("since") and v["created_at"][:10] < s["since"]:
                continue
            url = f"https://www.twitch.tv/videos/{v['id']}"
            aud = WORK / f"a{v['id']}.m4a"
            try:
                subprocess.run(["yt-dlp", "-q", "-f", "Audio_Only/bestaudio/worst", "-o", str(aud), url],
                               check=True, timeout=1500)
                pcm = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", str(aud), "-ac", "1", "-ar", "4000",
                                      "-f", "s16le", "-"], capture_output=True, check=True).stdout
            except Exception as e:  # noqa: BLE001
                print("audio download failed", v["id"], e)
                continue
            a = np.frombuffer(pcm, np.int16).astype(np.float32)
            sec = len(a) // 4000
            rms = np.sqrt((a[:sec * 4000].reshape(sec, 4000) ** 2).mean(1) + 1)
            db = 20 * np.log10(rms)
            base = np.array([np.median(db[max(0, i - 120):i + 1]) for i in range(sec)])
            win = np.convolve(db - base, np.ones(4) / 4, mode="same")  # 4 s of sudden loudness
            order = np.argsort(-win)
            picked = []
            for i in order:
                i = int(i)
                if win[i] < 6 or len(picked) >= per_vod:
                    break
                s0 = max(0, i - before)
                if any(abs(i - p) < 180 for p in picked) or _used_near(v["id"], s0):
                    continue
                picked.append(i)
                cands.append((s, {"id": f"v{v['id']}_{s0}", "url": url, "vod_start": s0, "vod_end": min(sec, i + after),
                                  "title": v["title"], "views": round(float(win[i]), 1), "loudness_jump_db":
                                  round(float(win[i]), 1), "duration": before + after, "created": v["created_at"],
                                  "streamer": v["user_name"]}))
            aud.unlink(missing_ok=True)
    cands.sort(key=lambda sc: sc[1]["views"], reverse=True)
    return cands[:6]


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

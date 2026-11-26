"""Step 2 (GitHub Actions): render the Short the Claude session chose, draft it in Postey, ping Telegram.

state/job.json (written by the daily Claude session):
{"clip_url": "...", "clip_id": "...", "source": "<name in config/sources.json>",
 "start": 0, "end": 30, "hook": "...", "youtube_title": "...", "description": "...", "tags": "a,b",
 "narration_url": "<public mp3 url or empty>", "pan": "0:0.5,8:0.8" }
"""
import base64
import json
import os
import pathlib
import subprocess
import sys

import requests

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import pipeline as P  # noqa: E402
from notify import notify  # noqa: E402

job = json.loads((P.ROOT / "state/job.json").read_text())
if job.get("done"):
    sys.exit("job already done")
src = next(s for s in P.CFG["sources"] if s["name"] == job["source"])
if not src.get("enabled") and not job.get("test"):
    P.fail(f"Quelle {src['name']} ist nicht freigegeben – nichts gepostet.")
if job["clip_id"] in P.POSTED:
    sys.exit("already posted")

raw, cut = P.WORK / "raw.mp4", P.WORK / "cut.mp4"
subprocess.run(["yt-dlp", "-q", "-f", "best[ext=mp4]/best", "-o", str(raw), job["clip_url"]], check=True)
start, end = float(job.get("start", 0)), float(job.get("end", 59))
subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-ss", str(start), "-i", str(raw), "-t", str(min(end - start, 59)),
                "-c:v", "libx264", "-crf", "16", "-c:a", "aac", str(cut)], check=True)
dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(cut)],
                           capture_output=True, text=True).stdout)

meta = P.WORK / "clip.json"
streamer = job.get("streamer") or src["login"]
meta.write_text(json.dumps({"best": {"title": job["hook"], "duration": dur, "broadcaster_name": streamer,
                                     "credit": src.get("credit")}}))
env, narr = dict(os.environ), ""
if src["mode"] == "campaign":
    env["LAYOUT"] = "campaign"
else:
    if job.get("narration_url"):
        narr = str(P.WORK / "narr.mp3")
        pathlib.Path(narr).write_bytes(requests.get(job["narration_url"], timeout=120).content)
    elif os.environ.get("ELEVENLABS_API_KEY") and job.get("narration_text"):
        narr = str(P.WORK / "narr.mp3")
        P.elevenlabs(job["narration_text"], pathlib.Path(narr))
    if job.get("pan"):
        env["PAN"] = job["pan"]
out = P.WORK / "short.mp4"
subprocess.run([sys.executable, str(P.ROOT / "shorts/make_short.py"), str(cut), str(meta), str(out), narr, job["hook"]],
               check=True, env=env)

key = os.environ.get("POSTEY_MCP_KEY")
if not key:
    P.fail("Short ist fertig, aber das GitHub-Secret POSTEY_MCP_KEY fehlt – bitte eintragen: "
           "https://app.postey.ai?settings=agents&section=advanced → New MCP key")
desc = job.get("description", "")
if src.get("credit"):
    desc += "\n\n" + src["credit"]
if src.get("required_hashtags"):
    desc += "\n" + " ".join(src["required_hashtags"])
p = P.Postey(key)
# Postey REST upload (same endpoint the official Postey CLI uses; MCP base64 is too small for video)
with open(out, "rb") as f:
    r = requests.post("https://srvr.postey.ai/v1/media/unlinked", headers={"X-API-Key": key},
                      files={"file": ("short.mp4", f, "video/mp4")}, data={"platform": "youtube"}, timeout=600)
print("upload", r.status_code, r.text[:300])
r.raise_for_status()
up = r.json()
up = up.get("data", up)
cdn = up.get("cdn_url") or up.get("url") or (up.get("cdn_urls") or [None])[0]
if not cdn:
    P.fail(f"Postey-Upload ohne URL: {str(up)[:200]}")
post = p.call("create_post", {"request": {
    "account_id": P.CFG["postey_account_id"], "platform": "YOUTUBE", "title": f"OWC {job['clip_id']}"[:50],
    "idempotency_key": f"owc-{job['clip_id']}",
    "contents": [{"youtube_title": job["youtube_title"][:100], "text": desc, "media_urls": [cdn],
                  "youtube_tags": job.get("tags", ""), "youtube_category_id": "20", "youtube_made_for_kids": False}]}})

P.POSTED.append(job["clip_id"])
P.POSTED_F.write_text(json.dumps(P.POSTED, indent=1))
job["done"] = True
(P.ROOT / "state/job.json").write_text(json.dumps(job, indent=1))
msg = ("🧪 TEST – NICHT veröffentlichen, nur ansehen & dann löschen!\n" if job.get("test") else "") + (f"🎬 Neues Short als Entwurf in Postey (Post {post.get('post_id') or post.get('id')}):\n{job['youtube_title']}\n"
       f"👉 In der Postey-App prüfen und veröffentlichen.")
if src["mode"] == "campaign" and src.get("submit_url"):
    msg += f"\n💰 Danach den YouTube-Link INNERHALB VON 30 MIN einreichen: {src['submit_url']}"
notify(msg)

"""Step 1 (GitHub Actions): list fresh clips of ALLOWED streamers + 4 preview frames each.

Writes output/candidates/candidates.json and output/candidates/<n>_<i>.jpg so the daily
Claude session can look at them and choose. No AI key needed here.
"""
import json
import pathlib
import shutil
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import pipeline as P  # noqa: E402

OUT = P.ROOT / "output/candidates"
shutil.rmtree(OUT, ignore_errors=True)
OUT.mkdir(parents=True)

# 1st choice: moments where the live chat exploded (needs chat logs from "GTA6 Chat Logger")
try:
    cands = P.vod_candidates()
except Exception as e:  # noqa: BLE001
    print("vod_candidates failed:", e)
    cands = []
if not cands:  # fallback: viewer-made Twitch/Kick clips
    cands = P.pick_clip()  # enabled sources only, not yet posted, NSFW titles filtered, top 5
result = []
for n, (src, clip) in enumerate(cands):
    raw = P.WORK / f"c{n}.mp4"
    try:
        dl = ["--download-sections", f"*{clip['vod_start']}-{clip['vod_end']}"] if "vod_start" in clip else []
        subprocess.run(["yt-dlp", "-q", *dl, "-f", "best[height<=1080][ext=mp4]/best[height<=1080]/best",
                        "-o", str(raw), clip["url"]], check=True, timeout=600)
        dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                    str(raw)], capture_output=True, text=True).stdout or clip["duration"])
        for i in range(4):
            subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-ss", str(dur * (i + 0.5) / 4), "-i", str(raw),
                            "-frames:v", "1", "-vf", "scale=480:-1", str(OUT / f"{n}_{i}.jpg")], check=True)
        text = P.transcript(raw)
    except Exception as e:  # noqa: BLE001
        print("skip", clip["url"], e)
        continue
    result.append({"n": n, "source": src["name"], "mode": src["mode"], "duration": dur, "transcript": text[:1500], **clip})

(OUT / "candidates.json").write_text(json.dumps(result, indent=2))
print(len(result), "candidates")

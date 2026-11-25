"""Turn a horizontal Twitch clip into an edited 9:16 YouTube Short.

- NO streamer face: full-height 9:16 gameplay crop placed away from the facecam
  (detected with OpenCV); any detected facecam inside the crop is blurred.
- NO streamer voice: the voice is removed with Demucs, game sound/music stays.
- Our ElevenLabs narration on top (game audio ducks under it), with word-by-word
  captions generated from the narration, hook title at the top, credit at the bottom.

Usage: python make_short.py <clip.mp4> <clip.json> <out.mp4> [narration.mp3] [hook text]
"""
import json
import os
import pathlib
import subprocess
import sys

import cv2
import numpy as np
from faster_whisper import WhisperModel

src, meta_path, out = sys.argv[1], sys.argv[2], sys.argv[3]
narration = sys.argv[4] if len(sys.argv) > 4 and sys.argv[4] else None
meta = json.loads(pathlib.Path(meta_path).read_text())["best"]
hook_text = sys.argv[5] if len(sys.argv) > 5 else meta["title"]
work = pathlib.Path(out).parent
work.mkdir(parents=True, exist_ok=True)


def run(*cmd):
    subprocess.run(cmd, check=True)


CAMPAIGN = os.environ.get("LAYOUT") == "campaign"  # keep facecam + original audio (campaign rules)

# --- 1. remove streamer voice ----------------------------------------------
if CAMPAIGN:
    game_audio = pathlib.Path(src)
else:
  run("ffmpeg", "-loglevel", "error", "-y", "-i", src, "-vn", "-ac", "2", "-ar", "44100", str(work / "orig.wav"))
  run(sys.executable, "-m", "demucs", "--two-stems", "vocals", "-n", "htdemucs", "-o", str(work / "sep"),
      str(work / "orig.wav"))
  game_audio = work / "sep" / "htdemucs" / "orig" / "no_vocals.wav"

# --- 2. find facecam & choose crop -------------------------------------------
cap = cv2.VideoCapture(src)
W, H = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
hits, samples = [], 16
for i in range(samples):
    cap.set(cv2.CAP_PROP_POS_FRAMES, int(n * (i + 0.5) / samples))
    ok, frame = cap.read()
    if not ok:
        continue
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    for (x, y, w, h) in cascade.detectMultiScale(gray, 1.1, 6, minSize=(H // 25, H // 25)):
        hits.append((x, y, w, h))
cap.release()

# a webcam sits in the same place in many frames; game faces move around
facecams = []
for b in hits:
    cx, cy = b[0] + b[2] / 2, b[1] + b[3] / 2
    same = [o for o in hits if abs(o[0] + o[2] / 2 - cx) < W * 0.04 and abs(o[1] + o[3] / 2 - cy) < H * 0.04]
    if len(same) >= samples * 0.35:
        # facecam boxes are usually ~3x the face size
        facecams.append((max(0, int(b[0] - b[2])), max(0, int(b[1] - b[3])), int(b[2] * 3), int(b[3] * 3)))
facecams = list({(round(f[0], -1), round(f[1], -1)): f for f in facecams}.values())

# cut off the bottom 18%: streamers often show live captions/alerts there
crop_h = int(H * 0.82) // 2 * 2
cw = int(crop_h * 9 / 16) // 2 * 2
center = (W - cw) // 2
best_x, best_cost = center, None
for x in range(0, W - cw + 1, 8):
    overlap = sum(max(0, min(x + cw, f[0] + f[2]) - max(x, f[0])) * f[3] for f in facecams)
    cost = (overlap, abs(x - center))
    if best_cost is None or cost < best_cost:
        best_x, best_cost = x, cost
print(f"facecams={facecams} crop_x={best_x}")

# optional camera pan: PAN="t:center_frac,t:center_frac,..." (e.g. "0:0.5,9:0.85,20:0.5")
# keeps the action in frame when it happens off-center; linear interpolation between keys
crop_x = str(best_x)
pan = os.environ.get("PAN")
if pan:
    keys = [(float(a), float(b)) for a, b in (k.split(":") for k in pan.split(","))]
    xs = [(t, min(max(int(f * W - cw / 2), 0), W - cw)) for t, f in keys]
    expr = str(xs[-1][1])
    for (t0, x0), (t1, x1) in reversed(list(zip(xs, xs[1:]))):
        expr = f"if(lt(t\\,{t1})\\,{x0}+({x1}-{x0})*(t-{t0})/{t1 - t0}\\,{expr})"
    crop_x = f"if(lt(t\\,{xs[0][0]})\\,{xs[0][1]}\\,{expr})"

blur = ""
for i, (fx, fy, fw, fh) in enumerate(facecams):
    fw, fh = min(fw, W - fx) // 2 * 2, min(fh, H - fy) // 2 * 2
    blur += f"[v{i}]split[v{i}a][v{i}b];[v{i}b]crop={fw}:{fh}:{fx}:{fy},boxblur=40:5[bl{i}];" \
            f"[v{i}a][bl{i}]overlay={fx}:{fy}[v{i + 1}];"
last = f"v{len(facecams)}"

# --- 3. captions from narration ---------------------------------------------
def ts(t: float) -> str:
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def esc(text: str) -> str:
    return text.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")


dur = float(meta["duration"])
events = []
cap_src = narration or (src if CAMPAIGN else None)
if cap_src:
    narration_for_caps = cap_src
    model = WhisperModel("small", device="cpu", compute_type="int8")
    pcm = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", narration_for_caps, "-f", "s16le", "-ac", "1",
                          "-ar", "16000", "-"], check=True, capture_output=True).stdout
    audio = np.frombuffer(pcm, np.int16).astype(np.float32) / 32768.0
    segs, _ = model.transcribe(audio, language="en", word_timestamps=True)
    try:
        words = [w for s in segs for w in (s.words or [])]
    except (IndexError, ValueError):  # no speech in the audio
        words = []
    chunk = []
    for w in words:
        chunk.append(w)
        if len(chunk) == 3 or w.word.strip().endswith((".", "!", "?", ",")):
            events.append(chunk)
            chunk = []
    if chunk:
        events.append(chunk)

hook = esc(hook_text).upper()
if len(hook) > 42:
    hook = hook[:40].rsplit(" ", 1)[0] + "…"
lines = [
    "[Script Info]", "ScriptType: v4.00+", "PlayResX: 1080", "PlayResY: 1920", "",
    "[V4+ Styles]",
    "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
    "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
    "MarginR, MarginV, Encoding",
    "Style: Hook,DejaVu Sans,78,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,1,0,0,0,100,100,0,0,1,7,0,8,60,60,200,1",
    "Style: Cap,DejaVu Sans,96,&H00FFFFFF,&H0000E5FF,&H00000000,&H80000000,1,0,0,0,100,100,0,0,1,8,3,2,60,60,620,1",
    "Style: Credit,DejaVu Sans,40,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,1,0,0,0,100,100,0,0,1,4,0,2,60,60,170,1",
    "", "[Events]",
    "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    f"Dialogue: 1,{ts(0)},{ts(dur)},Hook,,0,0,0,,{{\\fad(200,0)}}{hook}",
    f"Dialogue: 1,{ts(0)},{ts(dur)},Credit,,0,0,0,,{esc(meta.get('credit') or 'Gameplay: twitch.tv/' + meta['broadcaster_name'])}",
]
for ch in events:
    start, end = ch[0].start, max(ch[-1].end, ch[0].start + 0.35)
    k = "".join(f"{{\\kf{max(1, int((w.end - w.start) * 100))}}}{esc(w.word.strip()).upper()} " for w in ch)
    lines.append(f"Dialogue: 0,{ts(start)},{ts(end)},Cap,,0,0,0,,"
                 f"{{\\fscx80\\fscy80\\t(0,120,\\fscx100\\fscy100)}}{k.strip()}")
ass = work / "subs.ass"
ass.write_text("\n".join(lines) + "\n", encoding="utf-8")

# --- 4. render ----------------------------------------------------------------
if CAMPAIGN and facecams:
    # campaign layout: streamer facecam on top, gameplay below, original audio
    fx, fy, fw, fh = max(facecams, key=lambda f: f[2] * f[3])
    fh = min(fh, H - fy) // 2 * 2
    fw = min(max(fw, int(fh * 1080 / 608)), W - fx) // 2 * 2
    gh = H // 2 * 2
    gw = int(gh * 1080 / 1312) // 2 * 2
    gx = (W - gw) // 2
    vf = (f"[0:v]split[a][b];[a]crop={fw}:{fh}:{fx}:{fy},scale=1080:608:force_original_aspect_ratio=increase,"
          f"crop=1080:608[top];[b]crop={gw}:{gh}:{gx}:0,scale=1080:1312[bot];[top][bot]vstack,"
          f"subtitles={ass.as_posix()},format=yuv420p[v]")
elif CAMPAIGN:
    vf = (f"[0:v]crop={int(H * 9 / 16) // 2 * 2}:{H}:{(W - int(H * 9 / 16)) // 2}:0,scale=1080:1920,"
          f"subtitles={ass.as_posix()},format=yuv420p[v]")
else:
    vf = (f"[0:v]null[v0];{blur}[{last}]crop={cw}:{crop_h}:'{crop_x}':0,scale=1080:1920,"
          f"subtitles={ass.as_posix()},format=yuv420p[v]")
if narration:
    af = ("[1:a]volume=0.9[g];[2:a]volume=1.6,apad,asplit[n1][n2];"
          "[g][n1]sidechaincompress=threshold=0.03:ratio=8:attack=20:release=400[duck];"
          "[duck][n2]amix=inputs=2:duration=first:normalize=0,loudnorm=I=-14:TP=-1.5:LRA=11[a]")
    inputs = ["-i", src, "-i", str(game_audio), "-i", narration]
else:
    af = "[1:a]loudnorm=I=-14:TP=-1.5:LRA=11[a]"  # campaign: [1:a] is the original clip audio
    inputs = ["-i", src, "-i", str(game_audio)]
run("ffmpeg", "-loglevel", "error", "-y", *inputs, "-filter_complex", vf + ";" + af,
    "-map", "[v]", "-map", "[a]", "-t", str(min(dur, 59)), "-r", "30", "-c:v", "libx264", "-preset", "medium",
    "-crf", "23", "-maxrate", "4M", "-bufsize", "8M", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", out)
print("Done:", out)

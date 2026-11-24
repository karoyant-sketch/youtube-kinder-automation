"""Turn a horizontal Twitch clip into an edited 9:16 YouTube Short.

Edit: blurred full-screen background + sharp centered gameplay, hook title at the
top, word-by-word animated captions (faster-whisper), streamer credit at the bottom.
Usage: python make_short.py <clip.mp4> <clip.json> <out.mp4>
"""
import json
import pathlib
import subprocess
import sys

from faster_whisper import WhisperModel

src, meta_path, out = sys.argv[1], sys.argv[2], sys.argv[3]
meta = json.loads(pathlib.Path(meta_path).read_text())["best"]
work = pathlib.Path(out).parent
ass_path = work / "subs.ass"


def ts(t: float) -> str:
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def esc(text: str) -> str:
    return text.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")


# --- captions -------------------------------------------------------------
model = WhisperModel("small", device="cpu", compute_type="int8")
segments, _ = model.transcribe(src, language="en", word_timestamps=True, vad_filter=True)
words = [w for seg in segments for w in (seg.words or [])]

events = []
chunk = []
for w in words:
    chunk.append(w)
    if len(chunk) == 3 or w.word.strip().endswith((".", "!", "?", ",")):
        events.append(chunk)
        chunk = []
if chunk:
    events.append(chunk)

hook = esc(meta["title"]).upper()
if len(hook) > 42:
    hook = hook[:40].rsplit(" ", 1)[0] + "…"
credit = f"🎥 twitch.tv/{meta['broadcaster_name']}"
dur = float(meta["duration"])

lines = [
    "[Script Info]", "ScriptType: v4.00+", "PlayResX: 1080", "PlayResY: 1920", "",
    "[V4+ Styles]",
    "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
    "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
    "MarginR, MarginV, Encoding",
    "Style: Hook,DejaVu Sans,74,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,1,0,0,0,100,100,0,0,1,6,0,8,60,60,230,1",
    "Style: Cap,DejaVu Sans,92,&H00FFFFFF,&H0000E5FF,&H00000000,&H80000000,1,0,0,0,100,100,0,0,1,7,3,2,60,60,520,1",
    "Style: Credit,DejaVu Sans,44,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,1,0,0,0,100,100,0,0,1,4,0,2,60,60,150,1",
    "", "[Events]",
    "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    f"Dialogue: 1,{ts(0)},{ts(dur)},Hook,,0,0,0,,{{\\fad(200,0)}}{hook}",
    f"Dialogue: 1,{ts(0)},{ts(dur)},Credit,,0,0,0,,{esc(credit)}",
]
for ch in events:
    start, end = ch[0].start, max(ch[-1].end, ch[0].start + 0.35)
    # karaoke: active word turns yellow, pop-in scale animation
    k = "".join(f"{{\\kf{max(1, int((w.end - w.start) * 100))}}}{esc(w.word.strip()).upper()} " for w in ch)
    lines.append(f"Dialogue: 0,{ts(start)},{ts(end)},Cap,,0,0,0,,"
                 f"{{\\fscx80\\fscy80\\t(0,120,\\fscx100\\fscy100)}}{k.strip()}")
ass_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

# --- video ----------------------------------------------------------------
vf = (
    "[0:v]split[a][b];"
    "[a]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=24:2,eq=brightness=-0.12[bg];"
    "[b]scale=1080:-2,crop=1080:min(ih\\,1080)[fg];"
    "[bg][fg]overlay=(W-w)/2:(H-h)/2-40,"
    f"subtitles={ass_path.as_posix()},format=yuv420p[v]"
)
subprocess.run([
    "ffmpeg", "-y", "-i", src, "-filter_complex", vf, "-map", "[v]", "-map", "0:a?",
    "-t", "59", "-r", "30", "-c:v", "libx264", "-preset", "medium", "-crf", "20",
    "-c:a", "aac", "-b:a", "160k", "-af", "loudnorm=I=-14:TP=-1.5:LRA=11",
    "-movflags", "+faststart", out,
], check=True)
print("Done:", out)

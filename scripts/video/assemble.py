"""Assemble recorded frames + narration clips into an MP4 with captions.
usage: python assemble.py <videoDir> <out.mp4>"""
import json
import subprocess
import sys
from pathlib import Path

import imageio_ffmpeg

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
d = Path(sys.argv[1])
out = Path(sys.argv[2])
out.parent.mkdir(parents=True, exist_ok=True)
tl = json.loads((d / "timeline.json").read_text(encoding="utf-8"))
frames, scenes, end = tl["frames"], tl["scenes"], tl["end"]
t0 = frames[0]["t"]

# 1) frames -> concat list with real durations (screencast only sends changed frames)
lines = []
for i, f in enumerate(frames):
    nxt = frames[i + 1]["t"] if i + 1 < len(frames) else end
    lines.append(f"file '{(d / 'frames' / f['file']).as_posix()}'\nduration {max(0.001, nxt - f['t']):.4f}")
lines.append(f"file '{(d / 'frames' / frames[-1]['file']).as_posix()}'")  # concat demuxer quirk
(d / "frames.txt").write_text("\n".join(lines), encoding="utf-8")


# 2) captions (SRT): one cue per sentence, spread over the clip by length
def ts(sec):
    ms = int(round(sec * 1000))
    return f"{ms // 3600000:02}:{ms // 60000 % 60:02}:{ms // 1000 % 60:02},{ms % 1000:03}"


cues = []
for sc in scenes:
    start = sc["start"] - t0
    dur = sc["audio_ms"] / 1000
    parts = [p.strip() for p in sc["text"].replace("S K", "SK").replace("S C A I", "SCAI").split(". ") if p.strip()]
    total = sum(len(p) for p in parts)
    t = start
    for p in parts:
        span = dur * len(p) / total
        text = p if p.endswith((".", "?", "!")) else p + "."
        cues.append((t, t + span, text))
        t += span
srt = "\n".join(f"{i}\n{ts(a)} --> {ts(b)}\n{txt}\n" for i, (a, b, txt) in enumerate(cues, 1))
(d / "captions.srt").write_text(srt, encoding="utf-8")

# 3) audio: each clip delayed to its scene start, mixed into one track
inputs, filters = [], []
for i, sc in enumerate(scenes, start=1):
    inputs += ["-i", str(d / "audio" / f"{sc['id']}.mp3")]
    ms = int((sc["start"] - t0) * 1000) + 250
    filters.append(f"[{i}:a]adelay={ms}|{ms}[a{i}]")
mix = "".join(f"[a{i}]" for i in range(1, len(scenes) + 1))
filters.append(f"{mix}amix=inputs={len(scenes)}:normalize=0,afade=t=out:st={end - t0 - 1.2:.2f}:d=1.2[aout]")

# 4) video: constant 30 fps, H.264; burn captions (styled) for viewers with sound off
srt_path = (d / "captions.srt").as_posix().replace(":", "\\:")
style = ("FontName=Segoe UI,FontSize=15,PrimaryColour=&H00FFFFFF,OutlineColour=&H40000000,"
         "BorderStyle=3,Outline=5,Shadow=0,MarginV=26")  # BorderStyle 3 = opaque box behind text
vf = f"[0:v]fps=30,format=yuv420p,subtitles='{srt_path}':force_style='{style}'[vout]"
cmd = [FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", str(d / "frames.txt"), *inputs,
       "-filter_complex", ";".join([vf, *filters]), "-map", "[vout]", "-map", "[aout]",
       "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p", "-color_range", "tv",
       "-c:a", "aac", "-b:a", "160k", "-ar", "48000",
       "-movflags", "+faststart", "-shortest", str(out)]
r = subprocess.run(cmd, capture_output=True, text=True)
if r.returncode != 0:
    print(r.stderr[-3000:])
    sys.exit(1)
print(f"wrote {out} ({out.stat().st_size / 1e6:.1f} MB), {len(cues)} captions")

"""Generate narration audio per scene with a neural Canadian-English voice (edge-tts),
and record each clip's duration (ms) for the screen recorder."""
import asyncio
import json
import re
import subprocess
import sys
from pathlib import Path

import edge_tts
import imageio_ffmpeg

HERE = Path(__file__).parent
VOICE = sys.argv[1] if len(sys.argv) > 1 else "en-CA-ClaraNeural"
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()


def duration_ms(path: Path) -> int:
    out = subprocess.run([FFMPEG, "-i", str(path)], capture_output=True, text=True).stderr
    h, m, s = re.search(r"Duration: (\d+):(\d+):([\d.]+)", out).groups()
    return int((int(h) * 3600 + int(m) * 60 + float(s)) * 1000)


async def main() -> None:
    scenes = json.loads((HERE / "scenes.json").read_text(encoding="utf-8"))
    audio_dir = HERE / "audio"
    audio_dir.mkdir(exist_ok=True)
    total = 0
    for sc in scenes:
        mp3 = audio_dir / f"{sc['id']}.mp3"
        await edge_tts.Communicate(sc["text"], VOICE, rate="+4%").save(str(mp3))
        sc["audio_ms"] = duration_ms(mp3)
        total += sc["audio_ms"]
        print(f"{sc['id']:<12} {sc['audio_ms'] / 1000:5.1f}s")
    (HERE / "scenes_timed.json").write_text(json.dumps(scenes, indent=1), encoding="utf-8")
    print(f"total narration {total / 1000:.1f}s with {VOICE}")


asyncio.run(main())

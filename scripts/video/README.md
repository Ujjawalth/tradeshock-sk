# Demo video (AI-narrated screen recording)

Rebuilds `submission/TradeShock_SK_demo.mp4` from the running app.

- **Voice:** Microsoft neural text-to-speech (`en-CA-ClaraNeural`) via `edge-tts`.
- **Picture:** headless Edge screen-recording of the real app, driven scene by scene (Chrome DevTools Protocol screencast).
- **Assembly:** ffmpeg, with burned-in captions plus an `.srt` file.

The tools are only for making the video, not app dependencies, so install them in a separate environment:

```powershell
python -m venv vtools
vtools\Scripts\pip install edge-tts imageio-ffmpeg
```

Then, with the app running on http://127.0.0.1:5000:

```powershell
copy scripts\video\* <workdir>\                   # scenes.json, make_voice.py, record.mjs, assemble.py
vtools\Scripts\python <workdir>\make_voice.py en-CA-ClaraNeural   # 1. narration clips + durations
node <workdir>\record.mjs http://127.0.0.1:5000/ <workdir>        # 2. scene-by-scene screen recording
vtools\Scripts\python <workdir>\assemble.py <workdir> submission\TradeShock_SK_demo.mp4   # 3. MP4
```

- Edit `scenes.json` to change the narration (`text`) or what happens on screen (`action`, JavaScript run in the page).
- To show live AI in the video, unlock AI (API credit and password) and change scenes 04/09 to paste a real headline or ask a question.

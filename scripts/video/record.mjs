// Screen-record the live app scene by scene (CDP screencast), timed to the narration clips.
// usage: node record.mjs <url> <videoDir>
import { spawn } from "node:child_process";
import { writeFileSync, mkdirSync, readFileSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const [url, dir] = process.argv.slice(2);
const W = 1280, H = 720;
const scenes = JSON.parse(readFileSync(join(dir, "scenes_timed.json"), "utf8"));
const framesDir = join(dir, "frames");
rmSync(framesDir, { recursive: true, force: true });
mkdirSync(framesDir, { recursive: true });

const edge = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const port = 9400 + Math.floor(Math.random() * 400);
const profile = mkdtempSync(join(tmpdir(), "edgerec-"));
const proc = spawn(edge, ["--headless=new", "--disable-gpu", `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`,
  "--hide-scrollbars", `--window-size=${W},${H}`, "about:blank"], { stdio: "ignore" });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

let wsUrl;
for (let i = 0; i < 40 && !wsUrl; i++) {
  try { wsUrl = (await (await fetch(`http://127.0.0.1:${port}/json`)).json()).find((t) => t.type === "page")?.webSocketDebuggerUrl; }
  catch { await sleep(250); }
}
const ws = new WebSocket(wsUrl);
await new Promise((r) => ws.addEventListener("open", r));
let id = 0;
const pending = new Map();
const frames = [];
const errors = [];
ws.addEventListener("message", (ev) => {
  const msg = JSON.parse(ev.data);
  if (msg.id && pending.has(msg.id)) { pending.get(msg.id)(msg); pending.delete(msg.id); }
  if (msg.method === "Page.screencastFrame") {
    const n = String(frames.length).padStart(5, "0");
    const file = `f${n}.jpg`;
    writeFileSync(join(framesDir, file), Buffer.from(msg.params.data, "base64"));
    frames.push({ file, t: Date.now() / 1000 });
    ws.send(JSON.stringify({ id: ++id, method: "Page.screencastFrameAck", params: { sessionId: msg.params.sessionId } }));
  }
  if (msg.method === "Runtime.exceptionThrown") errors.push(msg.params.exceptionDetails.text);
});
const send = (method, params = {}) => new Promise((r) => { const i = ++id; pending.set(i, r); ws.send(JSON.stringify({ id: i, method, params })); });

await send("Runtime.enable");
await send("Page.enable");
await send("Emulation.setDeviceMetricsOverride", { width: W, height: H, deviceScaleFactor: 1, mobile: false });
await send("Page.navigate", { url });
await sleep(5000);  // page + baseline plan loaded

await send("Page.startScreencast", { format: "jpeg", quality: 85, maxWidth: W, maxHeight: H, everyNthFrame: 1 });
await sleep(600);
const timeline = [];
for (const sc of scenes) {
  timeline.push({ id: sc.id, start: Date.now() / 1000, audio_ms: sc.audio_ms, text: sc.text });
  const res = await send("Runtime.evaluate", { expression: sc.action, awaitPromise: false });
  if (res.result?.exceptionDetails) errors.push(`${sc.id}: ${res.result.exceptionDetails.text}`);
  await sleep(sc.audio_ms + 700);
}
await sleep(800);
await send("Page.stopScreencast");
const end = Date.now() / 1000;
writeFileSync(join(dir, "timeline.json"), JSON.stringify({ frames, scenes: timeline, end }, null, 1));
console.log(`recorded ${frames.length} frames, ${(end - timeline[0].start).toFixed(1)}s`);
if (errors.length) console.log("errors:", errors.join("\n"));
ws.close();
proc.kill();
process.exit(0);

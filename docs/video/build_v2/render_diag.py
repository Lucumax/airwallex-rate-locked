"""Render the two diagram scenes (d1 hook, d3 close) from stage.html, frame by frame at 30 fps, 1920x960, to _work/diag/<id>.mp4.
Also writes _work/diag_plan.json (durations and the local start of each narration line) for compose.py.
usage: render_diag.py [d1 d3]   (run with the playwright venv: <your Playwright venv>/python; FFMPEG env var points at ffmpeg)"""
import os, json, subprocess, sys
from pathlib import Path
from playwright.sync_api import sync_playwright
HERE = Path(__file__).resolve().parent; W = HERE / "_work"
FF = os.environ.get("FFMPEG", "ffmpeg")
clips = json.loads((W / "clips.json").read_text())
GAP, LEAD, TAIL = 0.35, {"d1": 0.3, "d3": 0.4}, {"d1": 0.7, "d3": 0.9}
plan = {}
for sid, n in (("d1", 2), ("d3", 1)):
    durs = [clips[f"{sid}_{i}"]["dur"] for i in range(n)]
    starts, cur = [], LEAD[sid]
    for d in durs:
        starts.append(cur); cur += d + GAP
    plan[sid] = {"lines": starts, "dur": starts[-1] + durs[-1] + TAIL[sid], "line_durs": durs}
(W / "diag_plan.json").write_text(json.dumps(plan, indent=1))
ids = sys.argv[1:] or ["d1", "d3"]
(W / "diag").mkdir(exist_ok=True)
with sync_playwright() as p:
    br = p.chromium.launch(channel="msedge", headless=True)
    pg = br.new_page(viewport={"width": 1920, "height": 960})
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto((HERE / "stage.html").as_uri()); pg.evaluate("window.ready")
    for sid in ids:
        L = plan[sid]["lines"]; fps = 30; n = int(plan[sid]["dur"] * fps)
        ff = subprocess.Popen([FF, "-y", "-loglevel", "error", "-f", "image2pipe", "-framerate", str(fps), "-c:v", "mjpeg", "-i", "-",
                               "-c:v", "libx264", "-preset", "medium", "-crf", "16", "-pix_fmt", "yuv420p", str(W / "diag" / f"{sid}.mp4")], stdin=subprocess.PIPE)
        for i in range(n):
            pg.evaluate("([id,t,L])=>render(id,t,L)", [sid, i / fps, L])
            ff.stdin.write(pg.screenshot(type="jpeg", quality=95))
        ff.stdin.close(); ff.wait()
        print(sid, n, "frames", round(plan[sid]["dur"], 1), "s", flush=True)
    print("page errors:", errs)
    br.close()

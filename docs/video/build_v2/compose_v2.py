"""Compose the sandbox cut v2 from three sources: the v1 take (_work/raw_v1: console.mp4 and phone.mp4, the live sandbox world), take N1 (_work/raw_n1:
console.mp4, the plan beats on the live sandbox world), and the two diagram cards (_work/diag/d1.mp4, d3.mp4). The Kokoro narration lines are placed at the
moments of the take they describe, and captions are burned in.
Outputs (docs/video/): rate_locked_sandbox_v2_4.mp4, .srt (v2.1: the Verify FAIL is on screen, takes are tagged where they join; v2.2: the opening promise
line, a labelled correction of an older console line in the 5 Nov take, and a 3 s hold of the end card, all in pass 2; v2.3: line u4_2 re-voiced, full recompose from the same takes; v2.4: the four lines with the brand name re-voiced with a phoneme override at their v2.3 lengths, pass 2 only), narration_sandbox_v2.txt (the spoken text), narration_sandbox_v2.wav (git-ignored)
Layout 1920x1080: a 960 px picture (console with a smooth camera, or phone-in-frame plus console panel, or a diagram card) and a 120 px caption band.
Sped-up stretches (waiting for the live sandbox or for the real clock) are cut from the same take and carry an on-screen label; nothing is faked and no
number is edited. Run with a TTS venv that has numpy, soundfile and PIL: <your TTS venv>/python compose_v2.py [--pass2]"""
import os, sys, json, subprocess, math
from pathlib import Path
import numpy as np, soundfile as sf
from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent; W = HERE / "_work"; OUT = HERE.parent
FF = os.environ.get("FFMPEG", "ffmpeg")
NAME = "rate_locked_sandbox_v2_4"
BG = (14, 19, 25)
M1 = json.loads((W / "rec_log_v1.json").read_text())["marks"]
MN = json.loads((W / "rec_log_n1.json").read_text())["marks"]
MARKS = {"v1": M1, "n1": MN}
CLIPFILE = {"v1": W / "raw_v1" / "console.mp4", "n1": W / "raw_n1" / "console.mp4"}
clips = json.loads((W / "clips.json").read_text())
narr = json.loads((HERE / "narration.json").read_text(encoding="utf-8"))
dplan = json.loads((W / "diag_plan.json").read_text())
COST = os.environ.get("COST_PHRASE", "Eleven cents less this time")
PHONE_OFF = M1["phone_offset"]
FONT_B = r"C:\Windows\Fonts\segoeuib.ttf"
F_LABEL = ImageFont.truetype(FONT_B, 26)
FONT_CAP = ImageFont.truetype(FONT_B, 44)


def ease(x): x = min(1, max(0, x)); return x * x * (3 - 2 * x)


def dur(lid): return clips[lid]["dur"]


# ------------------------------------------------------------------ segment plan
SEGS = []
def seg(**k):
    k.setdefault("src", "v1"); SEGS.append(k); return k

FIT = (960, 540, 2160)
FULL = (960, 480, 1920)          # full width, top 960 px of the page (header chips visible)
def cam(*kf): return list(kf)


def fast(a, b, why, base=3, target=2.6, **k):
    """a sped-up stretch of the same take: at least `base`x, and fast enough to last about `target` seconds; the label says how much"""
    S = max(base, round((b - a) / target))
    return seg(a=a, b=b, S=S, label=f"SPED UP {S}x: {why}", **k)

# where each spoken line starts: (take, mark) in a console take, or None for the diagram cards (cued inside the card)
ANCH = {"c0_0": None, "d1_0": None, "d1_1": None, "d3_0": None,
        "u1_0": ("v1", "u1_0"), "u1_1": ("v1", "u1_1"), "u1_2": ("v1", "u1_2"),
        "u2_0": ("n1", "u2_0"), "u2_1": ("n1", "u2_1"), "u2b_0": ("n1", "u2b_0"), "u2b_1": ("n1", "u2b_1", 0.45),
        "u3_0": ("n1", "lock_click", -0.5), "u3_1": ("v1", "u3_0"), "u3_2": ("v1", "u3_1", -0.8), "u3_3": ("v1", "u3_2", -1.1),
        "u4_0": ("v1", "u4_0", -1.6), "u4_1": ("v1", "u4_1"), "u4_2": ("v1", "cost_shown", -3.2), "u4_3": ("v1", "u4_3"),
        "u5_0": ("v1", "u5_0"), "u5_1": ("v1", "u5_1"), "u5_2": ("v1", "u5_2", 1.8)}  # u5_2 is placed so "it fails" lands as the FAIL appears
def anch(lid):
    a = ANCH[lid]
    return None if a is None else MARKS[a[0]][a[1]] + (a[2] if len(a) > 2 else 0.0)

# c0: the cold open, the refused screen of the v1 take (its own 4 s timeline), zoomed on the banner and the timeline (the recorder's pointer ring sits below this crop)
seg(k="c", src="v1", a=M1["stale_shown"] + 0.08, b=M1["stale_shown"] + 4.3, S=1, cam=cam((M1["stale_shown"], 960, 380, 1400, 0.01)), id_="c0")
# d1: rewind and the problem
seg(k="diag", id="d1")
# u1: the model read, the held invoice, a person resolves it (v1 take)
seg(k="c", a=M1["u1_0"] - 0.6, b=M1["u1_1"] - 0.3, S=1, cam=cam((M1["u1_0"] + 0.8, 960, 400, 1900, 1.0)))
seg(k="c", a=M1["u1_1"] - 0.3, b=M1["u1_2"] + 5.6, S=1, cam=cam((M1["u1_1"] + 0.2, 960, 430, 1500, 1.0), (M1["u1_1"] + 3.2, 1280, 600, 1400, 1.0), (M1["u1_2"] - 0.3, 1280, 620, 1400, 0.5)))
fast(M1["u1_2"] + 5.6, M1["u1_confirmed"] - 0.4, "waiting for the live model", k="c", base=4, target=1.9, cam=cam((0, 1280, 620, 1400, 0.1)))
seg(k="c", a=M1["plan_click"] - 1.1, b=M1["plan_click"] + 0.15, S=1, cam=cam((M1["plan_click"] - 1.1, 960, 425, 1700, 0.6)))
# u2: plan v1 (take N1), then a short look at the run chip in the footer (provenance of this take)
seg(k="c", src="n1", a=MN["plan_v1_ready"] - 0.15, b=MN["u2_1"] + dur("u2_1") + 0.45, S=1, cam=cam((0, *FULL, 0.01)),
    over="Run 2 of 2 (6 Nov): plan and recalculation. The invoices before were run 1 (5 Nov).", over_len=5.0)
seg(k="c", src="n1", a=MN["run_chip_shown"] + 0.05, b=MN["run_chip_shown"] + 1.45, S=1, cam=cam((0, 960, 705, 1500, 0.01)))
# u2b: recalculate only what changed (take N1): Demo controls, the press, the wait for the sandbox, plan v2, one zoom on the three tags
seg(k="c", src="n1", a=MN["u2b_0"] - 0.1, b=MN["lower_click"] + 0.5, S=1, cam=cam((0, *FULL, 0.01)))
fast(MN["lower_click"] + 0.5, MN["plan_v2_shown"] - 0.35, "waiting for the sandbox", src="n1", k="c", base=2, target=2.4, cam=cam((0, *FULL, 0.01)))
seg(k="c", src="n1", a=MN["plan_v2_shown"] - 0.35, b=MN["plan_v2_shown"] + 1.0, S=1, cam=cam((0, *FULL, 0.01)))
seg(k="c", src="n1", a=MN["plan_v2_shown"] + 1.0, b=anch("u2b_1") + dur("u2b_1") + 0.5, S=1, cam=cam((MN["plan_v2_shown"] + 1.0, 1000, 700, 980, 0.9)))
# the dark mode moment (the real toggle in the header). The screen recorder delivered the repaint late (the recording script was sleeping), so the page turns dark
# in the clip 2.2 s after the click mark: these are clip times found by measuring the clip, not mark times.
DARK_IN = MN["dark_on"] + 2.16
seg(k="c", src="n1", a=DARK_IN - 0.5, b=DARK_IN + 1.7, S=1, cam=cam((0, *FULL, 0.01)), over="Dark mode: follows the system setting, or one click")
# u3: the locked quote is requested (N1 shows the click and the sandbox issuing it), then the approver's phone (v1 take)
seg(k="c", src="n1", a=MN["lock_click"] - 0.6, b=MN["lock_click"] + 0.8, S=1, cam=cam((0, *FULL, 0.01)))
fast(MN["lock_click"] + 0.8, MN["approval_shown"] - 0.2, "waiting for the sandbox to issue the quote", src="n1", k="c", base=2, cam=cam((0, *FULL, 0.01)))
seg(k="c", src="n1", a=MN["approval_shown"] - 0.2, b=MN["approval_shown"] + 1.0, S=1, cam=cam((0, *FULL, 0.01)))
seg(k="split", a=M1["approval_shown"] - 0.2, b=anch("u4_0") + dur("u4_0") + 0.5, S=1,
    over="Back to run 1 (5 Nov): the approval and everything after use its own quote.", over_len=5.0)
fast(anch("u4_0") + dur("u4_0") + 0.5, M1["phone_expired"] - 1.2, "the real quote clock", k="split", base=5, target=4.0)
seg(k="split", a=M1["phone_expired"] - 1.2, b=M1["stale_shown"] - 0.3, S=1)
# u4: refused, the new quote
seg(k="c", a=M1["stale_shown"] - 0.3, b=M1["requote_click"] + 0.9, S=1, cam=cam((M1["stale_shown"] - 0.3, 960, 545, 1700, 0.5)))
seg(k="c", a=M1["requote_click"] + 0.9, b=M1["cost_shown"] - 0.4, S=1, cam=cam((0, 960, 545, 1700, 0.1)))
# the voice starts as the new quote is requested, so the cost sentence lands as the cost appears; the wait before the next approval is cut
seg(k="c", a=M1["cost_shown"] - 0.4, b=anch("u4_2") + dur("u4_2") + 0.4, S=1, cam=cam((M1["cost_shown"] - 0.4, 960, 545, 1700, 0.3), (M1["cost_shown"] + 0.3, 800, 470, 1300, 1.2)))
seg(k="split", a=M1["phone2_loaded"] - 0.4, b=M1["approved2"] + 1.6, S=1, panel=(300, 60))
seg(k="c", a=M1["approval2_shown"] - 0.2, b=M1["exec_click"] + 0.6, S=1, cam=cam((M1["approval2_shown"] - 0.2, 960, 425, 1700, 0.3)))
fast(M1["exec_click"] + 0.6, M1["record_shown"] - 0.3, "conversion and payout in the sandbox", k="c", base=3, cam=cam((0, 960, 425, 1700, 0.1)))
# u5: record, paid, verify
seg(k="c", a=M1["record_shown"] - 0.3, b=M1["u5_1"] - 0.2, S=1, cam=cam((M1["record_shown"] - 0.3, 960, 425, 1700, 0.4)))
e1 = M1["u5_1"] + dur("u5_1") + 0.3
seg(k="c", a=M1["u5_1"] - 0.2, b=e1, S=1, cam=cam((M1["u5_1"] - 0.2, 960, 700, 1500, 1.0)))
# the take idled on the unchanged record screen (a locator timeout) between the end of that line and the click: cut it
skip_to = max(e1, M1["paid_click"] - 1.4)
seg(k="c", a=skip_to, b=M1["paid_click"] + 0.4, S=1, cam=cam((0, 960, 700, 1500, 0.1)))
fast(M1["paid_click"] + 0.4, M1["paid_shown"] - 0.2, "waiting for the sandbox simulator", k="c", base=3, cam=cam((0, 960, 700, 1500, 0.1)))
seg(k="c", a=M1["paid_shown"] - 0.2, b=M1["verify_shown"] + 0.2, S=1, cam=cam((M1["paid_shown"] - 0.2, 960, 700, 1500, 0.2)))
# end on the FAIL, not before it: the take's raw video stops about 1 s after verify_fail, and the last frame is held so the named row stays on screen for 2.5 s
seg(k="c", a=M1["verify_shown"] + 0.2, b=M1["verify_fail"] + 2.5, S=1, cam=cam((M1["verify_shown"] + 0.2, 960, 700, 1600, 0.5)))
seg(k="diag", id="d3")
for s in SEGS:
    if s["k"] == "split": s["scr"] = True       # the approver's taps in the recording are made by the recording script

# camera continuity: a console shot that follows another console shot of the same take starts where the previous one ended
def _cam_continuity():
    prev, prev_src = None, None
    for s in SEGS:
        if s["k"] == "c":
            s["cam0"] = prev if (prev and prev_src == s["src"]) else (FULL if s["src"] == "n1" else FIT)
            kfs = s.get("cam") or []
            prev = cam_state(kfs, s["b"] if kfs and kfs[0][0] > 1 else 1e9, s["cam0"]) if kfs else s["cam0"]
            prev_src = s["src"]
        else:
            prev = None

# ------------------------------------------------------------------ final timeline
t = 0.0
for s in SEGS:
    s["dur"] = dplan[s["id"]]["dur"] if s["k"] == "diag" else (s["b"] - s["a"]) / s["S"]
    s["start"] = t; t += s["dur"]
TOTAL = t


def final_of(src, raw):
    """final time of a take moment inside a speed-1 segment of that take"""
    for s in SEGS:
        if s["k"] != "diag" and s["src"] == src and s["a"] - 1e-6 <= raw <= s["b"] + 1e-6:
            assert s["S"] == 1, f"narration line at {src} {raw} falls in a sped-up segment"
            return s["start"] + (raw - s["a"])
    raise KeyError((src, raw))

LINES = []   # (id, final start, dur, cap, over)
for sc in narr["scenes"]:
    for i, ln in enumerate(sc["lines"]):
        lid = f"{sc['id']}_{i}"
        d = clips[lid]["dur"]
        if sc["id"] in ("d1", "d3"):
            ds = next(s for s in SEGS if s["k"] == "diag" and s["id"] == sc["id"])
            st = ds["start"] + dplan[sc["id"]]["lines"][i]
        elif sc["id"] == "c0":
            cs = next(s for s in SEGS if s.get("id_") == "c0")
            st = cs["start"] + 0.35
        else:
            st = final_of(ANCH[lid][0], anch(lid))
        LINES.append((lid, st, d, ln["cap"], ln.get("over")))
LINES.sort(key=lambda x: x[1])
for (lid, st, d, *_), nx in zip(LINES, LINES[1:] + [(None, TOTAL + 9, 0)]):
    if st + d > nx[1] + 0.05: print("WARNING overlap", lid, round(st + d - nx[1], 2))
print("segments", len(SEGS), "total", round(TOTAL, 1), "s")
if "--plan" in sys.argv:      # print the timeline and stop (no rendering)
    for s in SEGS:
        print(f"{s['start']:6.1f} {s['dur']:5.1f} {s['k']:5} {s.get('src',''):3} {s.get('id', s.get('id_', '')) or ''} S={s.get('S')} {s.get('label', '')}")
    for l in LINES: print(l[0], round(l[1], 1), round(l[2], 1), "->", round(l[1] + l[2], 1))
    sys.exit(0)

# ------------------------------------------------------------------ frames
def src_frames(path, a, b, S, w, h, vf_extra=""):
    n = int(round((b - a) / S * 30))
    fps = 30.0 / S
    vf = f"fps={fps:.6f}" + (("," + vf_extra) if vf_extra else "")
    p = subprocess.Popen([FF, "-v", "error", "-ss", f"{max(a, 0):.3f}", "-t", f"{(b - a):.3f}", "-i", str(path), "-vf", vf, "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                         stdout=subprocess.PIPE, bufsize=10 ** 8)
    size = w * h * 3; last = None
    for i in range(n):
        buf = p.stdout.read(size)
        if len(buf) < size: buf = last if last is not None else bytes(size)
        last = buf
        yield np.frombuffer(buf, np.uint8).reshape(h, w, 3)
    p.stdout.close(); p.kill()


def cam_state(kfs, t, start=FIT):
    cur = start
    for (tk, cx, cy, w, d) in kfs:
        p = ease((t - tk) / max(d, 1e-6))
        if t < tk: break
        nxt = (cx, cy, w)
        prev = cur
        cur = tuple(prev[i] + (nxt[i] - prev[i]) * p for i in range(3))
        if p < 1: break
    return cur


def bezel():
    sw, sh, pad = 400, 866, 14
    ow, oh = sw + 2 * pad, sh + 2 * pad
    im = Image.new("RGBA", (ow, oh), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((0, 0, ow - 1, oh - 1), radius=46, fill=(40, 48, 58, 255), outline=(92, 104, 120, 255), width=3)
    m = Image.new("L", (ow, oh), 0)
    ImageDraw.Draw(m).rounded_rectangle((pad, pad, pad + sw - 1, pad + sh - 1), radius=32, fill=255)
    im.paste((0, 0, 0, 0), mask=m)
    return im, (sw, sh, pad)

BEZ, (SW, SH, PADB) = bezel()
PHONE_XY = (80, 64)           # outer bezel top-left inside the 960 px picture
PANEL_XY, PANEL_WH = (560, 64), (1320, 880)


def label(canvas, x, y, text, fill=(245, 158, 11)):
    ImageDraw.Draw(canvas).text((x, y), text, font=F_LABEL, fill=fill)


def phone_iter(s, n):
    """exactly n phone frames for this stretch: blank (None) before the phone page existed, then the clip, then its last frame"""
    pa = s["a"] - PHONE_OFF; pb = s["b"] - PHONE_OFF
    lead = int(round(max(0.0, -pa) / s["S"] * 30))
    for _ in range(min(lead, n)): yield None
    left = n - lead
    if left <= 0 or pb <= 0: return
    last = None
    g = src_frames(W / "raw_v1" / "phone.mp4", max(pa, 0), pb, s["S"], SW, SH, vf_extra=f"scale={SW}:{SH}:flags=lanczos")
    for i in range(left):
        try: last = next(g)
        except StopIteration: pass
        yield last


def split_frames(s):
    n = int(round(s["dur"] * 30))
    cs = src_frames(CLIPFILE["v1"], s["a"], s["b"], s["S"], 1920, 1080)
    phone = phone_iter(s, n)
    x0, y0 = s.get("panel", (300, 100))
    off_screen = Image.new("RGB", (SW, SH), (16, 21, 27))
    for i in range(n):
        c = next(cs)
        canvas = Image.new("RGB", (1920, 1080), BG)
        crop = Image.fromarray(c).crop((x0, y0, x0 + PANEL_WH[0], y0 + PANEL_WH[1]))
        canvas.paste(crop, PANEL_XY)
        ImageDraw.Draw(canvas).rectangle((PANEL_XY[0] - 2, PANEL_XY[1] - 2, PANEL_XY[0] + PANEL_WH[0] + 1, PANEL_XY[1] + PANEL_WH[1] + 1), outline=(92, 104, 120), width=2)
        pf = next(phone, None)
        canvas.paste(off_screen if pf is None else Image.fromarray(pf), (PHONE_XY[0] + PADB, PHONE_XY[1] + PADB))
        canvas.paste(BEZ, PHONE_XY, BEZ)
        label(canvas, PHONE_XY[0] + 4, 20, "APPROVER PAGE (phone size)")
        label(canvas, PANEL_XY[0], 20, "THE CONSOLE")
        yield canvas


def c_frames(s):
    n = int(round(s["dur"] * 30))
    cs = src_frames(CLIPFILE[s["src"]], s["a"], s["b"], s["S"], 1920, 1080)
    kfs = s.get("cam") or []
    for i in range(n):
        c = next(cs)
        rt = s["a"] + i * s["S"] / 30.0
        cx, cy, w = cam_state(kfs, rt, s["cam0"])
        if w <= 1920: cx = min(max(cx, w / 2), 1920 - w / 2)
        cy = min(max(cy, w / 4), 1080 - w / 4)
        kout = 1920.0 / w
        x0, y0 = cx - w / 2, cy - w / 4
        x1, y1 = x0 + w, y0 + w / 2
        vx0, vy0, vx1, vy1 = max(x0, 0), max(y0, 0), min(x1, 1920), min(y1, 1080)
        out = Image.new("RGB", (1920, 960), BG)
        dw, dh = max(1, round((vx1 - vx0) * kout)), max(1, round((vy1 - vy0) * kout))
        piece = Image.fromarray(c).resize((dw, dh), Image.BICUBIC, box=(vx0, vy0, vx1, vy1))
        out.paste(piece, (round((vx0 - x0) * kout), round((vy0 - y0) * kout)))
        canvas = Image.new("RGB", (1920, 1080), BG)
        canvas.paste(out, (0, 0))
        yield canvas


def d_frames(s):
    n = int(round(s["dur"] * 30))
    ds = src_frames(W / "diag" / (s["id"] + ".mp4"), 0, s["dur"], 1, 1920, 960)
    for i in range(n):
        canvas = Image.new("RGB", (1920, 1080), BG)
        canvas.paste(Image.fromarray(next(ds)), (0, 0))
        yield canvas

_cam_continuity()
# ------------------------------------------------------------------ pass 1: video
PASS2 = "--pass2" in sys.argv   # reuse _work/pass1.mp4 (video unchanged); redo audio, captions, labels and the final mux only
if not PASS2:
    PASS1 = W / "pass1.mp4"
    enc = subprocess.Popen([FF, "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", "1920x1080", "-r", "30", "-i", "-",
                            "-c:v", "libx264", "-preset", "veryfast", "-crf", "15", "-pix_fmt", "yuv420p", str(PASS1)], stdin=subprocess.PIPE)
    nf = 0
    for s in SEGS:
        gen = {"diag": d_frames, "c": c_frames, "split": split_frames}[s["k"]](s)
        for img in gen:
            enc.stdin.write(img.tobytes()); nf += 1
        print(" segment", s["k"], s.get("id", ""), s["src"] if s["k"] != "diag" else "", round(s["start"], 1), "+", round(s["dur"], 1), flush=True)
    enc.stdin.close(); enc.wait()
    print("frames", nf)


# ------------------------------------------------------------------ audio at final offsets
SR = 24000
mix = np.zeros(int((TOTAL + 1) * SR), dtype=np.float32)
for lid, st, d, cap, over in LINES:
    a, sr = sf.read(W / "audio" / f"{lid}.wav", dtype="float32")
    o = int(st * SR); mix[o:o + len(a)] += a
mix *= 0.89 / max(1e-6, np.abs(mix).max())
WAV = OUT / "narration_sandbox_v2.wav"
sf.write(WAV, mix[:int(TOTAL * SR)], SR)

# ------------------------------------------------------------------ captions (SRT sidecar and burned ASS)
MAXW = 1780
def split_fit(txt):
    if FONT_CAP.getlength(txt) <= MAXW: return [txt]
    words = txt.split(); best = len(words) // 2
    return split_fit(" ".join(words[:best])) + split_fit(" ".join(words[best:]))

caps = []
for lid, st, d, cap, over in LINES:
    cap = cap.replace("VARIANT_COST", COST)
    chunks = []
    for c in cap.split("|"): chunks += split_fit(c.strip())
    tot = sum(len(c) for c in chunks); cur = st
    for k, c in enumerate(chunks):
        dd = d * len(c) / tot
        caps.append((cur, cur + dd + (0.15 if k == len(chunks) - 1 else 0), c)); cur += dd

def ts(t, srt=True):
    h = int(t // 3600); m = int(t % 3600 // 60); s = t % 60
    return (f"{h:02d}:{m:02d}:{s:06.3f}".replace(".", ",")) if srt else f"{h}:{m:02d}:{s:05.2f}"
(OUT / (NAME + ".srt")).write_text("".join(f"{i+1}\n{ts(a)} --> {ts(b)}\n{c}\n\n" for i, (a, b, c) in enumerate(caps)), encoding="utf-8")
spoken_txt = ["Rate-Locked Approval, sandbox cut v2.4. Voice: Kokoro af_heart (synthetic), third person. Scenes c0, u1 to u5 are the real console against the Airwallex sandbox (test money); d1 and d3 are labelled diagram cards.\n"]
for sc in narr["scenes"]:
    ls = [clips[f"{sc['id']}_{i}"]["tts"] for i in range(len(sc["lines"]))]
    spoken_txt.append(f"[{sc['id']}] {sc['name']}\n" + "\n".join(ls))
(OUT / "narration_sandbox_v2.txt").write_text("\n\n".join(spoken_txt) + "\n", encoding="utf-8")

ass = ["[Script Info]", "ScriptType: v4.00+", "PlayResX: 1920", "PlayResY: 1080", "WrapStyle: 2", "",
       "[V4+ Styles]", "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding",
       "Style: Cap,Segoe UI,44,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,0,0,2,40,40,34,1",
       "Style: Tag,Segoe UI,32,&H00FFFFFF,&H00FFFFFF,&H000C41C2,&H000C41C2,-1,0,0,0,100,100,0,0,3,10,0,3,0,30,135,1",
       "Style: Scr,Segoe UI,30,&H00FFFFFF,&H00FFFFFF,&H000C41C2,&H000C41C2,-1,0,0,0,100,100,0,0,3,9,0,9,0,30,12,1",
       "Style: Over,Segoe UI,34,&H00FFFFFF,&H00FFFFFF,&H00251B14,&H00251B14,-1,0,0,0,100,100,0,0,3,14,0,2,0,0,150,1",
       "Style: Box,Segoe UI,20,&HFF000000,&HFF000000,&H000B9EF5,&H00000000,0,0,0,0,100,100,0,0,1,4,0,7,0,0,0,1",
       "", "[Events]", "Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text"]
for a, b, c in caps:
    ass.append(f"Dialogue: 0,{ts(a,False)},{ts(b,False)},Cap,,0,0,0,,{c}")
for s in SEGS:
    if s.get("label"):
        ass.append(f"Dialogue: 1,{ts(s['start'],False)},{ts(s['start']+s['dur'],False)},Tag,,0,0,0,,{s['label']}")
    if s.get("scr"):
        ass.append(f"Dialogue: 1,{ts(s['start'],False)},{ts(s['start']+s['dur'],False)},Scr,,0,0,0,,Scripted approver, not a person")
    if s.get("over"):
        ass.append(f"Dialogue: 1,{ts(s['start']+0.1,False)},{ts(s['start']+min(s['dur'], 0.1 + s.get('over_len', 1e9)),False)},Over,,0,0,0,,{s['over']}")
for lid, st, d, cap, over in LINES:
    if over:
        ass.append(f"Dialogue: 1,{ts(st+0.3,False)},{ts(st+d+1.0,False)},Over,,0,0,0,,{over}")
# v2.2 overlays (pass 2 only; no take, number or narration changes)
d1s = next(s for s in SEGS if s["k"] == "diag" and s["id"] == "d1")["start"]
PROMISE = "Rate-Locked Approval: an agent that may only spend at the rate a person approved."
ass.append(rf"Dialogue: 2,{ts(d1s + 5.8, False)},{ts(d1s + 10.0, False)},Over,,0,0,0,,{{\an5\pos(960,740)\fs46}}{PROMISE}")
# The 5 Nov take's console still shows an older sentence ("so a send cannot land on an expired quote"). The current code says the
# deadline is checked again right before the conversion is sent, which makes a late one very unlikely, not impossible: mark the line and say so.
sp = [s for s in SEGS if s["k"] == "split" and "panel" not in s]          # the first approval: the console panel sits at a fixed place
a0, a1 = sp[0]["start"], sp[-1]["start"] + sp[-1]["dur"]
BOX = r"{{\an7\pos(0,0)\p1\bord4\shad0\1a&HFF&\3c&H0B9EF5&}}m {0} {1} l {2} {1} l {2} {3} l {0} {3}{{\p0}}"
ass.append(f"Dialogue: 2,{ts(a0 + 0.05, False)},{ts(a1, False)},Box,,0,0,0,," + BOX.format(1342, 290, 1826, 350))   # from the first frame the line is shown
CORR = (r'Correction: this 5 Nov take shows older wording ("a send cannot land on an expired quote").\N'
        r'The deadline is checked again right before the conversion is sent: a late one is very unlikely, not impossible.')
ass.append(f"Dialogue: 2,{ts(a0 + 5.3, False)},{ts(a0 + 17.0, False)},Over,,0,0,0,,{CORR}")
ex0 = next(s for s in SEGS if s["k"] == "c" and s["src"] == "v1" and abs(s["a"] - (M1["approval2_shown"] - 0.2)) < 1e-6)
ex1 = next(s for s in SEGS if "conversion and payout" in s.get("label", ""))
ass.append(f"Dialogue: 2,{ts(ex0['start'] + 0.35, False)},{ts(ex1['start'] + ex1['dur'], False)},Box,,0,0,0,," + BOX.format(1098, 370, 1640, 434))
ass.append(f"Dialogue: 2,{ts(ex0['start'] + 0.35, False)},{ts(ex0['start'] + ex0['dur'], False)},Over,,0,0,0,,Older wording in this 5 Nov take: the deadline is now checked again right before the conversion is sent.")
(W / "captions.ass").write_text("\n".join(ass), encoding="utf-8")

# ------------------------------------------------------------------ pass 2: captions, loudness, mux
mp4 = OUT / (NAME + ".mp4")
subprocess.run([FF, "-y", "-loglevel", "error", "-i", "pass1.mp4", "-i", str(WAV), "-vf", "ass=captions.ass,tpad=stop_mode=clone:stop_duration=3",
                "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p", "-r", "30",
                "-af", "loudnorm=I=-16:TP=-1.5:LRA=11,apad", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-shortest", "-movflags", "+faststart", str(mp4)],
               check=True, cwd=str(W))
print("built", mp4, "total", round(TOTAL, 1), "s")
json.dump({"total": TOTAL, "segments": [{k: v for k, v in s.items() if k != "cam"} for s in SEGS],
           "lines": [{"id": l[0], "start": l[1], "dur": l[2]} for l in LINES]}, open(W / "final_timeline.json", "w"), indent=1)

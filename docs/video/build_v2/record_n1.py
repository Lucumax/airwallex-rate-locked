#!/usr/bin/env python3
"""Take N1 for the v2 cut: the plan screen of the REAL Rate-Locked console against the Airwallex SANDBOX world (live model reader),
plan only. Steps: resolve the Initech review, open the plan (plan v1, held), open Demo controls and press "Receipt R1 less certain"
(plan v2: Acme not recomputed, Globex recomputed with the same result, Initech DEFER to ESCALATE), hold on the three tags, open Details,
show the header Dark mode button for two seconds, then (a dry run for the optional "new money lands" line) press "Simulate deposit USD 1,000"
once and log what the plan did. No locked quote is requested: no conversion and no payout are created. A test deposit is test money.
usage: python record_n1.py <console_url>      (playwright venv). Writes _work/raw_n1/console.mp4 and _work/rec_log_n1.json."""
import base64, json, os, subprocess, sys, time
from pathlib import Path
from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
W = HERE / "_work"
RAW = W / "raw_n1"; RAW.mkdir(parents=True, exist_ok=True)
STILLS = W / "stills"; STILLS.mkdir(parents=True, exist_ok=True)
FF = os.environ.get("FFMPEG", "ffmpeg")
CVIEW = {"width": 1920, "height": 1080}
PVIEW = {"width": 390, "height": 844}
GAP = 0.55
clips = json.loads((W / "clips.json").read_text())
start_url = sys.argv[1]
BASE = start_url.split("/?")[0].rstrip("/")
if BASE.endswith("/"): BASE = BASE[:-1]
BASE = BASE.replace("/?", "")

INIT = (HERE / "pointer_init.js").read_text(encoding="utf-8")


def sleep(s): time.sleep(s)


class Sess:
    def __init__(self, ctx, page, view, scale):
        self.page, self.view = page, view
        self.cdp = ctx.new_cdp_session(page)
        self.frames = []
        self.cdp.on("Page.screencastFrame", self._frame)
        self.on = False
        self.mouse = (view["width"] // 2, view["height"] // 2)
        self.scale = scale

    def _frame(self, ev):
        self.frames.append((time.time(), base64.b64decode(ev["data"])))
        try: self.cdp.send("Page.screencastFrameAck", {"sessionId": ev["sessionId"]})
        except Exception: pass

    def start(self):
        self.frames = []
        self.cdp.send("Page.startScreencast", {"format": "jpeg", "quality": 92, "maxWidth": int(self.view["width"] * self.scale),
                                                "maxHeight": int(self.view["height"] * self.scale), "everyNthFrame": 1})
        self.on = True; self.t_start = time.time()
        self.page.mouse.move(*self.mouse)

    def stop_and_write(self, name, t_from, t1):
        self.cdp.send("Page.stopScreencast"); self.on = False
        self.page.wait_for_timeout(200)
        d = W / "frames" / name
        d.mkdir(parents=True, exist_ok=True)
        for f in d.glob("*"): f.unlink()
        fr = [f for f in self.frames if f[0] <= t1 + 0.2]
        for i, (ts, data) in enumerate(fr): (d / f"{i:05d}.jpg").write_bytes(data)
        times = [max(ts, t_from) for ts, _ in fr]
        with open(d / "list.txt", "w") as fh:
            for i in range(len(fr)):
                nxt = times[i + 1] if i + 1 < len(fr) else t1
                fh.write(f"file '{i:05d}.jpg'\nduration {max(nxt - times[i], 0.001):.4f}\n")
            fh.write(f"file '{len(fr)-1:05d}.jpg'\n")
        subprocess.run([FF, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(d / "list.txt"), "-vf", "fps=30,format=yuv420p",
                        "-c:v", "libx264", "-preset", "medium", "-crf", "16", str(RAW / f"{name}.mp4")], check=True)
        return len(fr)


class Rec:
    def __init__(self, ctxc, pagec, ctxp, pagep):
        self.page, self.pp = pagec, pagep
        self.c = Sess(ctxc, pagec, CVIEW, 1)
        self.p = Sess(ctxp, pagep, PVIEW, 2)
        self.marks = {}
        self.t0 = None

    def start(self):
        self.t0 = time.time(); self.c.start(); sleep(0.3)

    def mark(self, name, extra=None):
        self.marks[name] = round(time.time() - self.t0, 3)
        print(f"  [{self.marks[name]:7.2f}] {name}" + (f" {extra}" if extra else ""), flush=True)

    def phone_on(self):
        self.p.start(); self.marks["phone_offset"] = round(self.p.t_start - self.t0, 3); self.mark("phone_on")

    def say(self, sid, k, extra=0.0):
        now = time.time()
        self.mark(f"{sid}_{k}")
        dur = clips[f"{sid}_{k}"]["dur"]
        rec = self

        class Line:
            t0 = now
            def __call__(self):
                rec.until(now + dur + GAP + extra)
            def at(self, rel):
                rec.until(now + rel)
        return Line()

    def until(self, t):
        while time.time() < t: self.page.wait_for_timeout(30)

    def finish(self):
        t1 = time.time()
        self.marks["total"] = round(t1 - self.t0, 3)
        n = self.c.stop_and_write("console", self.c.t_start, t1)
        m = 0
        if self.p.on: m = self.p.stop_and_write("phone", self.p.t_start, t1)
        print(f"clips written: console {n} frames, phone {m} frames, total {self.marks['total']} s", flush=True)

    # ---- pointer
    def glide(self, x, y, secs=0.9, page=None):
        pg = page or self.page
        s = self.c if pg is self.page else self.p
        x0, y0 = s.mouse
        n = max(8, int(secs * 60))
        for i in range(1, n + 1):
            t = i / n; e = t * t * (3 - 2 * t)
            pg.mouse.move(x0 + (x - x0) * e, y0 + (y - y0) * e)
            sleep(secs / n)
        s.mouse = (x, y)

    def center(self, loc):
        b = self.box(loc)
        return b["x"] + b["width"] / 2, b["y"] + b["height"] / 2

    def click(self, loc, pause=0.3, glide=0.7, page=None):
        pg = page or self.page
        x, y = self.center(loc)
        self.glide(x, y, glide, pg)
        sleep(pause)
        pg.mouse.down(); sleep(0.09); pg.mouse.up()

    def box(self, loc):
        last = None
        for _ in range(20):
            b = loc.bounding_box()
            if b and last and abs(b["y"] - last["y"]) < 0.5 and abs(b["x"] - last["x"]) < 0.5: return b
            last = b; sleep(0.1)
        return last

    def hl(self, loc, pad=8, loc2=None, page=None):
        pg = page or self.page
        b = self.box(loc)
        if loc2 is not None:
            b2 = self.box(loc2)
            x0, y0 = min(b["x"], b2["x"]), min(b["y"], b2["y"])
            x1, y1 = max(b["x"] + b["width"], b2["x"] + b2["width"]), max(b["y"] + b["height"], b2["y"] + b2["height"])
            b = {"x": x0, "y": y0, "width": x1 - x0, "height": y1 - y0}
        pg.evaluate("""([x,y,w,h]) => { let d=document.getElementById('__rl_hl'); if(!d){d=document.createElement('div'); d.id='__rl_hl'; document.documentElement.appendChild(d);}
          Object.assign(d.style,{left:x+'px',top:y+'px',width:w+'px',height:h+'px',display:'block'}); }""",
                    [b["x"] - pad, b["y"] - pad, b["width"] + 2 * pad, b["height"] + 2 * pad])

    def hl_off(self, page=None):
        (page or self.page).evaluate("()=>{const d=document.getElementById('__rl_hl'); if(d) d.style.display='none';}")

    def snap(self, name, page=None):
        try: (page or self.page).screenshot(path=str(STILLS / f"{name}.png"))
        except Exception as e: print("  snap failed", name, e)


def main():
    out = {"what": "Take N1, plan only plus one quote request that is never approved, Airwallex sandbox world (test money), 2026-11-06. Seconds from the start of the console clip."}
    with sync_playwright() as p:
        br = p.chromium.launch(channel="msedge", headless=True)
        ctxc = br.new_context(viewport=CVIEW, bypass_csp=True)
        ctxc.add_init_script(INIT)
        page = ctxc.new_page()
        ctxp = br.new_context(viewport=PVIEW, device_scale_factor=2, bypass_csp=True)
        pp = ctxp.new_page()
        errs, posts = [], []
        page.on("pageerror", lambda e: errs.append("console: " + str(e)))
        page.on("response", lambda r: posts.append((r.request.method, r.url.replace(BASE, ""), r.status)) if r.request.method == "POST" else None)
        page.goto(start_url)
        page.wait_for_selector("#invoices-body tr.inv-row", timeout=120000)
        page.wait_for_function("document.querySelector('#invoices-body') && document.querySelector('#invoices-body').textContent.indexOf('Initech')>=0", timeout=120000)
        sleep(1.0)
        out["world_chip"] = page.locator("#chip-world").inner_text()
        assert out["world_chip"] == "WORLD: AIRWALLEX SANDBOX", out["world_chip"]
        out["reader_chip"] = page.locator("#chip-reader").inner_text()
        r = Rec(ctxc, page, ctxp, pp)
        r.start()
        try:
            r.mark("start")
            # resolve the Initech review (the model wait after the click is not shown in the cut)
            sel = page.locator("select[id^=rv-zone-]")
            r.click(sel, pause=0.2, glide=0.7)
            page.select_option("select[id^=rv-zone-]", value="+00:00")
            note = page.locator("input[id^=rv-note-]")
            r.click(note, pause=0.15, glide=0.5)
            page.type("input[id^=rv-note-]", "Checked with the supplier: times are UTC", delay=22)
            r.mark("confirm_click")
            r.click(page.locator("button", has_text="Confirm deadline").locator("visible=true").first, pause=0.15, glide=0.45)
            page.wait_for_function("/confirmed by a person/i.test(document.body.textContent)", timeout=150000, polling=100)
            sleep(0.8)
            r.mark("confirmed")
            pb = page.locator("a.button.primary", has_text="Plan payments").locator("visible=true").first
            r.click(pb, pause=0.3, glide=0.9)
            r.mark("plan_click")
            page.wait_for_function("document.querySelector('#plan-body .plan-table tbody tr') || (!document.getElementById('error').hidden && document.getElementById('error').textContent)", timeout=240000)
            perr = page.evaluate("(function(){var e=document.getElementById('error');return e&&!e.hidden?e.textContent:''})()")
            if perr: raise RuntimeError("console error before the plan: " + perr)
            sleep(0.5)
            page.evaluate("window.scrollTo(0,0)")
            r.mark("plan_v1_ready")
            out["plan_v1_text"] = page.locator("#plan-body").inner_text().replace("\n", " | ")[:1500]
            r.snap("n1_plan_v1")
            rows = page.locator("#plan-body .plan-table tbody tr")
            acme = rows.filter(has_text="Acme").first; globex = rows.filter(has_text="Globex").first; initech = rows.filter(has_text="Initech").first

            # ---- u2: plan v1, held while the voice names the three decisions
            w = r.say("u2", 0)
            r.glide(*r.center(page.locator("#plan-body .banner")), 0.9)
            r.hl(page.locator("#plan-body .banner"), pad=4)
            w(); r.hl_off()
            w = r.say("u2", 1)
            r.glide(*r.center(acme.locator("td").nth(3)), 0.7); r.hl(acme, pad=2)
            w.at(1.79); r.hl_off()
            r.glide(*r.center(globex.locator("td").nth(3)), 0.7); r.hl(globex, pad=2)
            w.at(4.92); r.hl_off()
            r.glide(*r.center(initech.locator("td").nth(3)), 0.6); r.hl(initech, pad=2)
            w(); r.hl_off()
            # footer: the run chip (provenance of this take)
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)"); sleep(1.0)
            out["run_chip"] = page.locator("#chip-run").inner_text()
            r.mark("run_chip_shown"); r.snap("n1_run_chip"); sleep(1.0)
            page.evaluate("window.scrollTo(0,0)"); sleep(0.4)

            # ---- u2b: Demo controls, "Receipt R1 less certain", plan v2, the three tags
            w = r.say("u2b", 0)
            r.mark("demo_open_click")
            page.locator("#plan-demo summary").scroll_into_view_if_needed(); sleep(0.5)
            r.click(page.locator("#plan-demo summary"), pause=0.2, glide=0.9)
            sleep(0.4)
            btn = page.locator("#btn-lower")
            btn.scroll_into_view_if_needed(); sleep(0.5)
            w.at(2.5)
            r.mark("lower_click")
            r.click(btn, pause=0.3, glide=0.7)
            page.wait_for_function("document.body.textContent.indexOf('Plan v2')>=0", timeout=15000)
            r.mark("plan_v2_shown")
            page.evaluate("window.scrollTo({top:0,behavior:'smooth'})")
            sleep(0.9)
            out["plan_v2_text"] = page.locator("#plan-body").inner_text().replace("\n", " | ")[:2000]
            r.snap("n1_plan_v2")
            w();
            w = r.say("u2b", 1)
            rows2 = page.locator("#plan-body .plan-table tbody tr")
            for nm, who, cut in (("acme", "Acme", 1.4), ("globex", "Globex", 3.93), ("initech", "Initech", None)):
                row = rows2.filter(has_text=who).first
                r.mark("tag_" + nm)
                r.glide(*r.center(row.locator("td").nth(3)), 0.7); r.hl(row, pad=2)
                if cut: w.at(cut)
                else: w()
                r.hl_off()
            r.mark("tags_done")
            # Details: the "Recomputed 2 of 3 obligations" line
            page.evaluate("document.getElementById('plan-details').open=true")
            sleep(0.4)
            page.locator("#plan-details").scroll_into_view_if_needed(); sleep(1.6)
            r.mark("details_shown"); r.snap("n1_details")
            page.evaluate("document.getElementById('plan-details').open=false")
            page.evaluate("window.scrollTo(0,0)"); sleep(0.7)

            # ---- dark mode for two seconds (header button), then back to light
            tg = page.locator("#theme-toggle")
            r.click(tg, pause=0.25, glide=0.8)
            r.mark("dark_on"); sleep(2.6); r.snap("n1_dark_plan_v2")
            r.mark("dark_hold_end")
            r.click(tg, pause=0.15, glide=0.4)
            r.mark("dark_off"); sleep(1.0)

            # ---- Globex: request a locked quote (the quote is never approved; no money moves)
            globex2 = rows2.filter(has_text="Globex").first
            btn2 = globex2.locator("button", has_text="locked quote").first
            r.glide(*r.center(globex2.locator("td").nth(3)), 0.6); sleep(0.3)
            r.mark("lock_click")
            r.click(btn2, pause=0.25, glide=0.7)
            page.wait_for_function("location.hash==='#approval' && document.querySelector('#approval-body article')", timeout=120000)
            r.mark("approval_shown")
            sleep(1.8)
            r.snap("n1_approval")

            # ---- dry run for the optional line: one test deposit on the plan screen, then what the plan did
            page.evaluate("location.hash='#plan'"); sleep(1.0)
            page.evaluate("document.getElementById('plan-demo').open=true")
            dep = page.locator("#btn-deposit")
            dep.scroll_into_view_if_needed(); sleep(0.3)
            r.mark("deposit_click")
            r.click(dep, pause=0.3, glide=0.8)
            try:
                page.wait_for_function("document.body.textContent.indexOf('Plan v3')>=0", timeout=25000)
                out["deposit_made_plan_v3"] = True
            except Exception:
                out["deposit_made_plan_v3"] = False
            sleep(1.0)
            page.evaluate("window.scrollTo(0,0)")
            out["after_deposit_text"] = page.locator("#plan-body").inner_text().replace("\n", " | ")[:2500]
            out["balances_after_deposit"] = page.locator("#plan-body .strip, #plan-body .bal").first.inner_text() if page.locator("#plan-body .bal").count() else ""
            out["error_banner_after_deposit"] = page.evaluate("(function(){var e=document.getElementById('error');return e&&!e.hidden?e.textContent:''})()")
            r.mark("deposit_done"); r.snap("n1_after_deposit")
            sleep(1.0)
        except Exception as e:
            import traceback; traceback.print_exc()
            out["error"] = repr(e); r.mark("error")
            try: r.snap("n1_error")
            except Exception: pass
        r.mark("end")
        out["posts"] = posts; out["errors"] = errs
        print("page errors:", errs); print("POSTs:", posts)
        r.finish()
        out["marks"] = r.marks
        (W / "rec_log_n1.json").write_text(json.dumps(out, indent=1))
        br.close()


main()

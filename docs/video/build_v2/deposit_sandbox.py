"""One test deposit for the optional "new money lands" line: on the sandbox world, resolve the Initech review, open the plan, press
"Simulate deposit USD 1,000" once (a test deposit, test money, POST /simulation/deposit/create), and log what the plan and the balances did.
No locked quote, no conversion, no payout. usage: python deposit_sandbox.py <console_url>   (playwright venv). Writes _work/deposit_sandbox.json"""
import json, sys, time
from pathlib import Path
from playwright.sync_api import sync_playwright

W = Path(__file__).resolve().parent / "_work"
url = sys.argv[1]
out = {"what": "One test deposit on the Airwallex sandbox world, test money, 2026-11-06"}
with sync_playwright() as p:
    br = p.chromium.launch(channel="msedge", headless=True)
    page = br.new_context(viewport={"width": 1920, "height": 1080}, bypass_csp=True).new_page()
    posts = []
    page.on("response", lambda r: posts.append((r.request.method, r.url.split("/api/")[-1], r.status)) if r.request.method == "POST" else None)
    page.goto(url)
    page.wait_for_function("document.querySelector('#invoices-body') && document.querySelector('#invoices-body').textContent.indexOf('Initech')>=0", timeout=180000)
    out["world_chip"] = page.locator("#chip-world").inner_text()
    page.select_option("select[id^=rv-zone-]", value="+00:00")
    page.fill("input[id^=rv-note-]", "Checked with the supplier: times are UTC")
    page.locator("button", has_text="Confirm deadline").locator("visible=true").first.click()
    page.wait_for_function("/confirmed by a person/i.test(document.body.textContent)", timeout=180000)
    page.locator("a.button.primary", has_text="Plan payments").locator("visible=true").first.click()
    page.wait_for_function("document.querySelector('#plan-body .plan-table tbody tr')", timeout=240000)
    time.sleep(1)
    def snap():
        return {"title": page.locator("#plan-body .plan-table").locator("xpath=preceding-sibling::*[1]").inner_text() if False else page.evaluate("(document.querySelector('#plan-body h3')||document.querySelector('#plan-body .panel-head')||{textContent:''}).textContent"),
                "balances": page.evaluate("[].map.call(document.querySelectorAll('#plan-body .bal'),function(n){return n.textContent.replace(/\s+/g,' ').trim()})"),
                "decisions": page.evaluate("[].map.call(document.querySelectorAll('#plan-body .plan-table tbody tr'),function(n){return n.textContent.replace(/\s+/g,' ').trim().slice(0,170)})"),
                "reserve": page.evaluate("(document.querySelector('#plan-body .reserve')||{textContent:''}).textContent.replace(/\s+/g,' ').trim()")}
    out["before"] = snap()
    page.evaluate("document.getElementById('plan-demo').open=true")
    btn = page.locator("#btn-deposit")
    btn.scroll_into_view_if_needed()
    out["button_disabled"] = btn.is_disabled()
    t0 = time.time()
    btn.click()
    try:
        page.wait_for_function("document.body.textContent.indexOf('Plan v2')>=0", timeout=120000)
        out["plan_v2_after_deposit"] = True
    except Exception:
        out["plan_v2_after_deposit"] = False
    out["seconds_to_answer"] = round(time.time() - t0, 1)
    time.sleep(1)
    out["after"] = snap()
    out["error_banner"] = page.evaluate("(function(){var e=document.getElementById('error');return e&&!e.hidden?e.textContent:''})()")
    out["posts"] = posts
    br.close()
(W / "deposit_sandbox.json").write_text(json.dumps(out, indent=1))
print(json.dumps(out, indent=1)[:3000])

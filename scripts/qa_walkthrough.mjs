// Headless walkthrough of the whole console story on the local fake. Node 22+ (global WebSocket), no packages.
//
//   node scripts/qa_walkthrough.mjs --out docs/screenshots [--chrome PATH] [--python python] [--theme dark] [--stage]
//
// By default it starts its own fresh console (python -m rate_locked serve, FAKE world) on a free loopback port and
// stops it at the end, because the console keeps its state in memory and the story can only be played once per
// server. To drive a server you started yourself, pass --url http://127.0.0.1:8770/ (it must be fresh).
//
// Drives a private headless Chromium (chrome-headless-shell or any Chrome/Edge with --headless) over the DevTools
// protocol. It never touches an already-open browser. Saves one PNG per step at desktop width, a phone-width set,
// and prints PASS/FAIL checks (story assertions, no horizontal overflow, no page errors). Exit code 1 on any FAIL.
import { spawn } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, writeFileSync, readdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const argv = process.argv.slice(2);
const opt = (k, d) => { const i = argv.indexOf(k); return i >= 0 ? argv[i + 1] : d; };
const GIVEN_URL = opt("--url", "");
const OUT = opt("--out", "docs/screenshots");
const THEME = opt("--theme", "");
const STAGE = argv.includes("--stage");
const SANDBOX = argv.includes("--sandbox");   // the five hero screens from the Airwallex sandbox world (test money, one conversion, one payout); keys come from the environment
const READER = opt("--reader", "scripted");
const DESK = opt("--desktop", "1920x1080").split("x").map(Number);   // desktop viewport; the hero screens must fit it above the fold
const VIDEO = opt("--video", "");   // folder: also save 1920x1080 (phone: 390x844); desktop frames are 1920x886 so the caption bars fit a 1920x1080 video frames and frames.json for scripts/make_video.py
const UNTIL = opt("--until", "");   // sandbox only: "newquote" stops after the new-quote screen, before anything is executed (no conversion, no payout)
const MEASURE = opt("--measure", "");   // file: after each screenshot, measure the WCAG contrast of every visible text and chip on the page and write the table (JSON)
const measured = [];
const frames = [];
mkdirSync(OUT, { recursive: true });
let ORIGIN = "";
let serverProc = null;
async function startServer() {
  if (GIVEN_URL) { ORIGIN = new URL(GIVEN_URL).origin; return; }
  const port = Number(opt("--port", 8800 + Math.floor(Math.random() * 190)));
  const env = { ...process.env, PYTHONPATH: "src" };
  // the live sandbox sometimes drops a call during start-up, and the console then exits with a printable reason: start again
  for (let attempt = 1; attempt <= (SANDBOX ? 6 : 1); attempt++) {
    let exited = false;
    serverProc = spawn(opt("--python", process.env.PYTHON || "python"), ["-m", "rate_locked", "serve", "--port", String(port), ...(SANDBOX ? ["--world", "sandbox", "--reader", READER] : [])], { env, stdio: "ignore" });
    serverProc.on("exit", () => { exited = true; });
    for (let i = 0; i < (SANDBOX ? 1000 : 60) && !exited; i++) {   // the sandbox preflight and the three beneficiaries can take a minute
      await sleep(250);
      try { const r = await fetch(`http://127.0.0.1:${port}/static/app.css`); if (r.ok) { ORIGIN = `http://127.0.0.1:${port}`; return; } } catch { /* not up yet */ }
    }
    if (!exited) { try { serverProc.kill(); } catch { /* gone */ } }
    if (SANDBOX) console.log(`      console start attempt ${attempt} did not come up${exited ? " (it exited)" : ""}`);
  }
  throw new Error("the console server did not start");
}

function findChrome() {
  const given = opt("--chrome", process.env.CHROME_PATH);
  if (given) return given;
  const home = process.env.USERPROFILE || process.env.HOME || "";
  const pw = join(home, "AppData", "Local", "ms-playwright");
  if (existsSync(pw)) {
    for (const d of readdirSync(pw).filter((n) => n.startsWith("chromium_headless_shell")).sort().reverse()) {
      const p = join(pw, d, "chrome-headless-shell-win64", "chrome-headless-shell.exe");
      if (existsSync(p)) return p;
    }
  }
  const guesses = ["C:/Program Files/Google/Chrome/Application/chrome.exe",
    "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe", "/usr/bin/chromium", "/usr/bin/google-chrome"];
  const hit = guesses.find(existsSync);
  if (!hit) throw new Error("No headless Chrome found. Pass --chrome PATH.");
  return hit;
}

const withTheme = (u) => (THEME ? u + (u.includes("?") ? "&" : "?") + "theme=" + THEME : u);   // the approver link carries the theme too, so a dark run shows both pages dark
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const results = [];
function check(name, ok, detail = "") {
  results.push({ name, ok });
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? "  " + detail : ""}`);
}

class Cdp {
  constructor(ws) { this.ws = ws; this.id = 0; this.waiting = new Map(); this.listeners = [];
    ws.addEventListener("message", (m) => { const d = JSON.parse(m.data);
      if (d.id && this.waiting.has(d.id)) { const { res, rej } = this.waiting.get(d.id); this.waiting.delete(d.id);
        d.error ? rej(new Error(d.error.message)) : res(d.result); }
      else this.listeners.forEach((f) => f(d)); }); }
  send(method, params = {}, sessionId) {
    const id = ++this.id;
    return new Promise((res, rej) => { this.waiting.set(id, { res, rej });
      this.ws.send(JSON.stringify({ id, method, params, ...(sessionId ? { sessionId } : {}) })); });
  }
  on(f) { this.listeners.push(f); }
}

class Page {
  constructor(cdp, sessionId, label) { this.cdp = cdp; this.sid = sessionId; this.label = label; this.errors = []; }
  send(m, p) { return this.cdp.send(m, p, this.sid); }
  async ev(expr) {
    const r = await this.send("Runtime.evaluate", { expression: expr, awaitPromise: true, returnByValue: true });
    if (r.exceptionDetails) throw new Error(`${this.label}: ${r.exceptionDetails.exception?.description || r.exceptionDetails.text}`);
    return r.result.value;
  }
  async size(w, h, mobile = false) {
    await this.send("Emulation.setDeviceMetricsOverride", { width: w, height: h, deviceScaleFactor: mobile ? 2 : 1, mobile });
  }
  async goto(url) { await this.send("Page.navigate", { url }); await sleep(700); }
  async until(expr, ms = 8000, what = expr) {
    const t = Date.now();
    while (Date.now() - t < ms) { if (await this.ev(`!!(${expr})`)) return true; await sleep(120); }
    throw new Error(`${this.label}: timed out waiting for ${what}`);
  }
  // click the first visible button or link whose text contains `text`
  async click(text, ms = 8000) {
    const js = `(function(){var t=${JSON.stringify(text)};var n=[].slice.call(document.querySelectorAll('button,a.button,summary')).filter(function(x){return x.offsetParent!==null&&!x.disabled&&x.textContent.indexOf(t)>=0})[0];if(n){n.click();return true}return false})()`;
    await this.until(js.replace("n.click();return true", "return true"), ms, `button "${text}"`);
    await this.ev(js);
    await sleep(450);
  }
  // wait for a link matching `sel` (optionally with text `rx`) and return its href
  async href(sel, rx = "") {
    const js = `(function(){var a=[].filter.call(document.querySelectorAll(${JSON.stringify(sel)}),function(x){return ${rx ? `/${rx}/.test(x.textContent)` : "true"}})[0];return a?a.getAttribute('href'):null})()`;
    await this.until(`(${js})`, 8000, `link ${sel}`);
    return this.ev(js);
  }
  async hash(id) { await this.ev(`location.hash=${JSON.stringify("#" + id)}`); await sleep(350); }
  async type(sel, value) {
    await this.ev(`(function(){var n=document.querySelector(${JSON.stringify(sel)});n.value=${JSON.stringify(value)};n.dispatchEvent(new Event('input',{bubbles:true}));n.dispatchEvent(new Event('change',{bubbles:true}))})()`);
  }
  async text(sel) { return this.ev(`(document.querySelector(${JSON.stringify(sel)})||{}).textContent||''`); }
  // WCAG 2.x contrast of every visible text node against the background actually painted behind it (alpha composited up the tree);
  // disabled controls are exempt; text of 24px or more (or 18.66px bold) needs 3:1, all other text 4.5:1
  async audit(name) {
    const r = await this.ev(`(function(){
      function parse(c){var m=c.match(/rgba?[(]([^)]+)[)]/);if(!m)return null;var p=m[1].split(/[ ,/]+/).filter(Boolean).map(Number);return {r:p[0],g:p[1],b:p[2],a:p.length>3?p[3]:1}}
      function lin(v){v/=255;return v<=0.03928?v/12.92:Math.pow((v+0.055)/1.055,2.4)}
      function lum(c){return 0.2126*lin(c.r)+0.7152*lin(c.g)+0.0722*lin(c.b)}
      function over(f,b){return {r:f.r*f.a+b.r*(1-f.a),g:f.g*f.a+b.g*(1-f.a),b:f.b*f.a+b.b*(1-f.a),a:1}}
      function bgOf(n){var stack=[];for(var e=n;e;e=e.parentElement){var c=parse(getComputedStyle(e).backgroundColor);if(c&&c.a>0){stack.push(c);if(c.a>=1)break}}
        var base=stack.length&&stack[stack.length-1].a>=1?stack.pop():{r:255,g:255,b:255,a:1};while(stack.length)base=over(stack.pop(),base);return base}
      var out=[],walker=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT);
      while(walker.nextNode()){var t=walker.currentNode;if(!t.nodeValue.trim())continue;var e=t.parentElement;if(!e)continue;
        var cs=getComputedStyle(e);if(cs.visibility==='hidden'||cs.display==='none'||e.closest('[hidden],.sr-only,script,style'))continue;
        var rc=e.getBoundingClientRect();if(rc.width<2||rc.height<2||rc.bottom<0||rc.top>innerHeight)continue;
        if(e.closest('button:disabled,[disabled]'))continue;
        var fg=parse(cs.color),bg=bgOf(e);if(!fg)continue;fg=over(fg,bg);
        var a=lum(fg),b=lum(bg),ratio=(Math.max(a,b)+0.05)/(Math.min(a,b)+0.05);
        var px=parseFloat(cs.fontSize),bold=parseInt(cs.fontWeight,10)>=700,large=px>=24||(px>=18.66&&bold);
        out.push({text:t.nodeValue.trim().slice(0,40),ratio:Math.round(ratio*100)/100,need:large?3:4.5,chip:!!e.closest('.chip,.tag,.status-chip'),tag:e.tagName})}
      return JSON.stringify(out)})()`);
    const rows = JSON.parse(r);
    const min = (xs) => xs.length ? Math.min(...xs.map((x) => x.ratio)) : null;
    const chips = rows.filter((x) => x.chip), text = rows.filter((x) => !x.chip);
    const fails = rows.filter((x) => x.ratio < x.need);
    const worst = (xs) => xs.slice().sort((a, b) => a.ratio - b.ratio).slice(0, 3).map((x) => `${x.ratio} "${x.text}"`);
    measured.push({ screen: name, theme: THEME || "default", texts: text.length, min_text: min(text), chips: chips.length, min_chip: min(chips), fails: fails.length, worst_text: worst(text), worst_chip: worst(chips), failing: fails.slice(0, 8).map((x) => `${x.ratio}<${x.need} "${x.text}"`) });
    if (MEASURE) writeFileSync(MEASURE, JSON.stringify(measured, null, 1));
    console.log(`      contrast ${name}: min text ${min(text)}, min chip ${min(chips)}, ${fails.length} below AA`);
  }
  async shot(name, { full = false, min = 700 } = {}) {
    if (MEASURE) await this.audit(name);
    const h = full ? await this.ev("Math.max(document.documentElement.scrollHeight,document.body.scrollHeight)") : 0;
    const w = await this.ev("window.innerWidth");
    const vh = await this.ev("window.innerHeight");
    const mobile = w < 600;
    if (full) await this.size(w, Math.min(Math.max(h, min), 9000), mobile);
    await sleep(150);
    const r = await this.send("Page.captureScreenshot", { format: "png" });
    writeFileSync(join(OUT, name + ".png"), Buffer.from(r.data, "base64"));
    if (full) await this.size(w, vh, mobile);
    console.log("      shot " + name + ".png");
  }
  // one video frame: scroll `sel` to the top (below the sticky bar) and capture the viewport only
  async frame(name, caption, secs, sel = "", kind = "desktop") {
    if (!VIDEO) return;
    mkdirSync(VIDEO, { recursive: true });
    if (sel) await this.ev(`(function(){var n=document.querySelector(${JSON.stringify(sel)});if(n){n.scrollIntoView({block:'start'});window.scrollBy(0,-72)}})()`);
    else await this.ev("window.scrollTo(0,0)");
    await sleep(300);
    const r = await this.send("Page.captureScreenshot", { format: "png" });
    writeFileSync(join(VIDEO, name + ".png"), Buffer.from(r.data, "base64"));
    frames.push({ file: name + ".png", caption, secs, kind });
  }
  // the content of a screen, up to its Details summary, ends inside the viewport
  async fits(id, label) {
    const f = JSON.parse(await this.ev(`(function(){var s=document.getElementById(${JSON.stringify(id)});var d=s.querySelector('details.details');var b=d?d.getBoundingClientRect().top:s.getBoundingClientRect().bottom;return JSON.stringify({bottom:Math.round(b),h:window.innerHeight})})()`));
    check(`${label} fits above the fold`, f.bottom <= f.h, `${f.bottom} of ${f.h}px`);
  }
  async overflow() {
    return this.ev("document.documentElement.scrollWidth - document.documentElement.clientWidth");
  }
}

// The five hero screens from the Airwallex sandbox world: test money, one conversion, one payout.
// Needs AIRWALLEX_CLIENT_ID, AIRWALLEX_API_KEY and RATE_LOCKED_BENEFICIARIES in the environment (never printed).
async function sandboxStory(con, open) {
  check("world chip says AIRWALLEX SANDBOX with the Test money tag", (await con.text("#chip-world")) === "WORLD: AIRWALLEX SANDBOX" && (await con.text("#chip-money")) === "Test money");
  const needsReview = await con.ev("!!document.querySelector('select[id^=rv-zone-]')");
  await con.fits("invoices", "sandbox invoices screen");
  await con.shot("01-invoices");
  if (needsReview) {
    await con.type("select[id^=rv-zone-]", "+00:00");
    await con.type("input[id^=rv-note-]", "Checked with the supplier: times are UTC");
    await con.click("Confirm deadline");
  }
  await con.hash("plan");
  await con.until("document.querySelector('#plan-body .plan-table tbody tr') || (!document.getElementById('error').hidden && document.getElementById('error').textContent)", 180000, "plan rows");
  const planErr = await con.ev("(function(){var e=document.getElementById('error');return e&&!e.hidden?e.textContent:''})()");
  if (planErr) throw new Error("the console showed an error before the plan: " + planErr);
  check("plan from the live rate: recommendation, balances, live rate line", await con.ev("!!document.querySelector('#plan-body .banner') && /Live Airwallex rate/.test(document.querySelector('#plan-body .rateline').textContent)"));
  await con.fits("plan", "sandbox plan screen");
  await con.shot("04-plan");
  // one conversion: Globex, quote valid 1 minute
  await con.ev(`(function(){var c=[].filter.call(document.querySelectorAll('#plan-body .plan-table tbody tr'),function(x){return x.textContent.indexOf('Globex')>=0})[0];[].filter.call(c.querySelectorAll('button'),function(b){return b.textContent.indexOf('locked quote')>=0})[0].click()})()`);
  await con.until("location.hash==='#approval' && document.querySelector('#approval-body article')", 60000, "approval screen");
  const link = await con.href("#approval-body a.button.primary");
  const app = await open(withTheme(ORIGIN + link), "approver");
  await app.size(390, 844, true);
  await app.until("document.getElementById('terms').children.length>0", 6000, "approver terms");
  check("approver phone: amounts, locked rate, fee, Approve and Decline visible without scrolling", await app.ev("document.getElementById('deny').getBoundingClientRect().bottom <= window.innerHeight && document.getElementById('raterow').textContent.indexOf('LOCKED')>=0"));
  await app.shot("08-approver-phone-live");
  // let the 1 minute quote lapse in real time, then approve anyway: refused locally, zero conversion and payout calls
  await app.until("/expired/i.test(document.getElementById('message').textContent)", 90000, "the approver page to show the expired quote");
  await app.ev("(function(){var b=document.getElementById('approve');b.disabled=false;b.click()})()");
  await sleep(1200);
  await con.hash("stale");
  await con.until("document.querySelector('#stale-body .alert')", 8000, "stale refusal");
  const st = (await con.text("#stale-body")).replace(/\s+/g, " ");
  check("stale approval: No conversion or payout was sent to Airwallex (0 execution API calls), counter 0", /No conversion or payout was sent to Airwallex \(0 execution API calls\)/.test(st) && (await con.text("#exec-calls")) === "0");
  await con.fits("stale", "sandbox stale refused screen");
  await con.shot("11-stale-refused");
  await con.click("Get a new quote");
  await con.until("document.querySelector('#stale-body .cost-big')", 30000, "cost difference");
  const cost = (await con.text("#stale-body .cost-big")).trim();
  check("the new quote shows the computed cost difference as it is", /^Costs [A-Z]{3} [\d,.]+ (more|less)$/.test(cost), cost);
  await con.fits("stale", "sandbox new-quote screen");
  await con.shot("12-stale-new-quote");
  if (UNTIL === "newquote") return;
  // approve the new terms, one conversion and one payout, PAID
  const link2 = await con.href("#stale-body a.button.primary", "Ask approver again");
  const app2 = await open(withTheme(ORIGIN + link2), "approver-2");
  await app2.size(390, 844, true);
  await app2.until("document.getElementById('terms').children.length>0", 6000, "new approver terms");
  await app2.shot("13-approver-phone-new-terms");
  await app2.ev("document.getElementById('approve').click()");
  await app2.until("/approved/i.test(document.getElementById('result').textContent+document.getElementById('message').textContent)", 8000, "approved");
  await con.hash("approval");
  await con.click("Execute these exact terms");
  await con.until("location.hash==='#record'", 60000, "record screen");
  await con.until("document.querySelector('#record-body table')", 60000, "record table");
  await con.click("Simulate transfer PAID");
  await con.until("[].some.call(document.querySelectorAll('#record-body .statline .chip'),function(c){return c.textContent==='PAID'})", 60000, "the transfer to read PAID");
  await sleep(500);
  const rec = (await con.text("#record-body")).replace(/\s+/g, " ");
  check("record: N of N checks passed, PASS banner bound to the verify result", /(\d+) of \1 checks passed/.test(rec) && await con.ev("document.getElementById('verify-banner').getAttribute('data-verdict')==='PASS' && /PASS/.test(document.getElementById('verify-banner').textContent)"));
  check("record names the Airwallex sandbox references", /Airwallex sandbox references/.test(rec));
  await con.fits("record", "sandbox record screen");
  await con.shot("15-record");
}

async function main() {
  await startServer();
  const chrome = findChrome();
  const profile = mkdtempSync(join(tmpdir(), "rl-qa-"));
  const port = 9300 + Math.floor(Math.random() * 500);
  const proc = spawn(chrome, ["--headless=new", "--no-sandbox", "--disable-gpu", "--hide-scrollbars", `--remote-debugging-port=${port}`,
    `--user-data-dir=${profile}`, "--no-first-run", "--window-size=1920,1080", "about:blank"], { stdio: "ignore" });
  let browserWs = null;
  for (let i = 0; i < 80 && !browserWs; i++) {
    await sleep(250);
    try { browserWs = (await (await fetch(`http://127.0.0.1:${port}/json/version`)).json()).webSocketDebuggerUrl; } catch { /* not up yet */ }
  }
  if (!browserWs) { proc.kill(); throw new Error("headless browser did not start"); }
  const ws = new WebSocket(browserWs);
  await new Promise((r) => ws.addEventListener("open", r));
  const cdp = new Cdp(ws);
  const pages = [];
  async function open(url, label) {
    const { targetId } = await cdp.send("Target.createTarget", { url: "about:blank" });
    const { sessionId } = await cdp.send("Target.attachToTarget", { targetId, flatten: true });
    const p = new Page(cdp, sessionId, label);
    await p.send("Page.enable"); await p.send("Runtime.enable");
    p.targetId = targetId;
    pages.push(p);
    await p.size(VIDEO ? 1920 : DESK[0], VIDEO ? 886 : DESK[1]);
    await p.goto(url);
    return p;
  }
  cdp.on((d) => {
    if (d.method === "Runtime.exceptionThrown") pages.forEach((p) => { if (p.sid === d.sessionId) p.errors.push(d.params.exceptionDetails.text + " " + (d.params.exceptionDetails.exception?.description || "")); });
    if (d.method === "Runtime.consoleAPICalled" && d.params.type === "error") pages.forEach((p) => { if (p.sid === d.sessionId) p.errors.push("console.error " + JSON.stringify(d.params.args.map((a) => a.value))); });
  });

  const q = (STAGE ? "?stage=video" : "?") + (THEME ? (STAGE ? "&" : "") + "theme=" + THEME : "");
  const consoleUrl = ORIGIN + "/" + (q === "?" ? "" : q);
  let con, app;
  try {
    con = await open(consoleUrl, "console");
    await con.until("document.querySelector('#invoices-body tr.inv-row')", SANDBOX ? 90000 : 8000, "invoices");
    story: {
    if (SANDBOX) { await sandboxStory(con, open); break story; }

    await sleep(600);
    const shownError = await con.ev("(function(){var e=document.getElementById('error');return e&&!e.hidden?e.textContent:''})()");
    check("console shows no error banner after load", shownError === "", shownError);
    check("guide line is filled in", (await con.text("#guide")).includes("Guide:"), (await con.text("#guide")).slice(0, 80));
    // ---- 1 invoices and the review flag --------------------------------------------------------------
    check("one world chip, a Test money tag, the reader chip here, the run id in the footer",
      (await con.text("#chip-world")) === "WORLD: FAKE" && (await con.text("#chip-money")) === "Test money" && /READER: TEST DOUBLE/.test(await con.text("#chip-reader")) &&
      /^RUN: LIVE/.test(await con.text("#chip-run")) && await con.ev("!document.getElementById('chip-reader').hidden && !!document.querySelector('footer #chip-run')"));
    check("plan waits for the review (Lower and Deposit disabled)", await con.ev("document.getElementById('btn-lower').disabled && document.getElementById('btn-deposit').disabled"));
    check("invoice that needs review is listed first, with its reason and the quoted source line", await con.ev("/NEEDS REVIEW/.test(document.querySelector('#invoices-body tr.inv-row td:last-child').textContent) && /no time zone/i.test(document.getElementById('inspector').textContent) && /Payment due/.test(document.querySelector('#inspector .srcquote').textContent)"));
    await con.fits("invoices", "invoices screen");
    await con.shot("01-invoices-review-needed");
    await con.frame("v01-invoices-top", "Three fictional invoices come in. WORLD: FAKE, test money, scripted TEST DOUBLE reader. Nothing here calls Airwallex or a model.", 13);
    await con.frame("v02-source-words", "Every field is shown with the exact source words it came from. Code finds the words, computes the offsets and checks the value.", 11, "#invoices-body");
    await con.frame("v03-review-needed", "Initech's due time has no time zone. The reader assumed UTC; code holds the invoice and the plan waits for a person.", 14, "#inspector");
    await con.type("select[id^=rv-zone-]", "+00:00");
    await con.type("input[id^=rv-note-]", "Checked with the supplier: times are UTC");
    await con.shot("02-review-form-filled", { full: false });
    await con.click("Confirm deadline");
    await con.until("document.body.textContent.indexOf('confirmed by a person')>=0", 6000, "HUMAN confirmation");
    check("review resolved: HUMAN line shown, row READY, plan v1 built", (await con.text("#invoices-detail-body")).includes("Ready for planning as O3") && await con.ev("![].some.call(document.querySelectorAll('#invoices-body tr.inv-row'),function(r){return /NEEDS REVIEW/.test(r.textContent)})"));
    await con.shot("03-review-resolved");
    await con.frame("v04-review-resolved", "A person chose the zone and wrote a note. The answer is labelled HUMAN and kept in the run. Only now is plan v1 built.", 9, "#inspector");

    // ---- 2 plan v1 to v3 -------------------------------------------------------------------------------
    await con.hash("plan");
    await con.until("document.querySelector('#plan-body .plan-table tbody tr')", 6000, "plan rows");
    const v1 = await con.text("#plan-body");
    check("plan v1 is FUND / CONVERT / DEFER", /FUND/.test(v1) && /CONVERT/.test(v1) && /DEFER/.test(v1));
    check("plan decisions sit above their inputs, which are collapsed in Details", await con.ev("!!(document.getElementById('plan-body').compareDocumentPosition(document.getElementById('plan-inputs')) & 4) && !document.getElementById('plan-details').open"));
    check("plan shows the recommendation, the balances, the rate line and one primary action", await con.ev("!!document.querySelector('#plan-body .banner') && document.querySelectorAll('#plan-body .bal').length===3 && !!document.querySelector('#plan-body .rateline') && document.querySelectorAll('#plan-body .plan-table button.primary').length===1"));
    await con.fits("plan", "plan screen");
    await con.shot("04-plan-v1");
    await con.frame("v05-plan-v1", "Code, not a model, decides each payment: fund, convert, defer or escalate, from balances, indicative rates and expected receipts.", 12, "#plan-body");
    await con.ev("document.getElementById('plan-demo').open=true");
    await con.click("Receipt R1 less certain");
    await con.until("document.body.textContent.indexOf('Plan v2')>=0", 6000, "plan v2");
    const v2 = await con.text("#plan-body");
    check("plan v2: Initech escalates, Acme not recomputed", /ESCALATE/.test(v2) && /Not recomputed/.test(v2));
    await con.shot("05-plan-v2-confidence-lowered");
    await con.frame("v06-plan-v2", "Lower the confidence of the expected receipt: Initech escalates to a person; Acme is reused untouched ('Not recomputed').", 11, "#plan-body");
    await con.ev("document.getElementById('plan-demo').open=true");
    await con.click("Simulate deposit USD 1,000");
    await con.until("document.body.textContent.indexOf('Plan v3')>=0", 6000, "plan v3");
    check("plan v3: deposit turns the escalation into a conversion", /Changed from[^.]*ESCALATE[^.]*to[^.]*CONVERT/.test((await con.text("#plan-body")).replace(/\s+/g, " ")));
    await con.shot("06-plan-v3-deposit-simulated");
    await con.frame("v07-plan-v3", "A simulated deposit (sandbox simulation endpoint, fake here) turns the escalation into a conversion. Only affected decisions recompute.", 11, "#plan-body");

    // ---- 3 approval at a locked quote -----------------------------------------------------------------
    await con.ev(`(function(){var c=[].filter.call(document.querySelectorAll('#plan-body .plan-table tbody tr'),function(x){return x.textContent.indexOf('Globex')>=0})[0];[].filter.call(c.querySelectorAll('button'),function(b){return b.textContent.indexOf('locked quote')>=0})[0].click()})()`);
    await con.until("location.hash==='#approval' && document.querySelector('#approval-body article')", 6000, "approval screen");
    const link = await con.href("#approval-body a.button.primary");
    const apTxt = await con.text("#approval-body");
    check("approval request shows quote id, rate, you pay, valid to, digest", ["Quote ID", "Locked rate", "You pay", "Quote valid to", "Digest"].every((s) => apTxt.includes(s)));
    await con.shot("07-approval-locked-quote");
    await con.frame("v08-approval", "For Globex the agent gets a locked FX quote. The approver sees one set of exact terms, in words and numbers, with the quote's end time.", 11, "#approval-body article");
    app = await open(withTheme(ORIGIN + link), "approver");
    await app.size(390, 844, true);
    await app.until("document.getElementById('terms').children.length>0", 6000, "approver terms");
    check("approver page is live and shows the 8-character digest prefix", (await app.text("#prefix")).length === 8);
    const fit = await app.ev(`(function(){var b=document.getElementById('deny').getBoundingClientRect();var d=document.getElementById('trade').textContent+document.getElementById('raterow').textContent+document.getElementById('decision').textContent;
      return JSON.stringify({bottom:Math.round(b.bottom),h:window.innerHeight,labels:['You pay','Supplier gets','LOCKED','Payout fee','Beneficiary'].every(function(l){return d.indexOf(l)>=0})&&/1 [A-Z]{3} = /.test(d)&&!!document.getElementById('why').textContent})})()`);
    const fitv = JSON.parse(fit);
    check("approver phone 390x844: amounts, locked rate, countdown, fee, beneficiary, Approve and Decline are visible without scrolling", fitv.labels && fitv.bottom <= fitv.h, fit);
    check("approver main card has no digest typing; the digest is behind Full terms", await app.ev("!document.getElementById('typed') && !!document.querySelector('#fullterms #prefix') && !document.getElementById('fullterms').open"));
    await app.shot("08-approver-phone-live", { min: 844 });
    await app.frame("v09-approver-live", "The approver page is separate. It can approve or deny this one request, with the quote's clock running.", 8, "", "phone");

    await con.click("Skip the fake clock ahead 61 s");
    await sleep(2500);                       // approver page polls every few seconds
    await app.until("/expired/i.test(document.getElementById('message').textContent)", 9000, "approver expired");
    await app.shot("09-approver-phone-expired", { min: 844 });
    await app.frame("v10-approver-expired", "Time cut: the fake clock was moved 61 s (a FAKE-world test control, logged). The quote has expired.", 7, "", "phone");
    check("approver page shows the expired state with an empty ring", await app.ev("/expired/i.test(document.getElementById('state-chip').textContent)"));
    await app.ev("(function(){var b=document.getElementById('approve');b.disabled=false;b.click()})()");
    await sleep(900);
    await app.shot("10-approver-phone-refused", { min: 844 });
    await app.frame("v11-approver-refused", "Approving anyway: the server re-checks the time and refuses.", 6, "#confirm", "phone");
    await con.hash("stale");
    await con.until("document.querySelector('#stale-body .alert')", 8000, "stale refusal");
    const st = (await con.text("#stale-body")).replace(/\s+/g, " ");
    check("stale approval: calm alert 'No conversion or payout was sent to Airwallex (0 execution API calls)', counter 0, timeline from real events", /after the quote expired/.test(st) && /No conversion or payout was sent to Airwallex \(0 execution API calls\)/.test(st) && (await con.text("#exec-calls")) === "0" && /Quote expired/.test(st) && /Refused/.test(st));
    await con.fits("stale", "stale refused screen");
    await con.shot("11-stale-refused");
    await con.frame("v12-stale", "Nothing was converted. The transport log counts zero conversion calls and zero payout calls.", 12, "#stale-body");

    await con.click("FAKE market move");
    await con.click("Get a new quote");
    await con.until("document.querySelector('#stale-body .cost-big')", 6000, "cost cards");
    check("new quote shows old and new rate and the computed cost difference", /Costs [A-Z]{3} [\d,.]+ (more|less)/.test(await con.text("#stale-body .cost-big")) && (await con.ev("document.querySelectorAll('#stale-body .rate-big').length")) === 2);
    await con.fits("stale", "stale new-quote screen");
    await con.shot("12-stale-new-quote-cost-difference");
    await con.frame("v13-new-quote", "After a market move on the fake, the agent gets a new quote and shows exactly what the new terms cost.", 10, "#stale-body");

    // ---- 4 approve the new terms, execute, record -------------------------------------------------------
    const link2 = await con.href("#stale-body a.button.primary", "Ask approver again");
    const app2 = await open(withTheme(ORIGIN + link2), "approver-2");
    await app2.size(390, 844, true);
    await app2.until("document.getElementById('terms').children.length>0", 6000, "new approver terms");
    await app2.shot("13-approver-phone-new-terms", { min: 844 });
    await app2.ev("document.getElementById('approve').click()");
    await app2.until("/approved/i.test(document.getElementById('result').textContent+document.getElementById('message').textContent)", 6000, "approved");
    await app2.shot("14-approver-phone-approved", { min: 844 });
    await app2.frame("v14-approved", "The new terms are approved once. The link cannot be reused.", 6, "#confirm", "phone");
    await con.hash("approval");
    await con.click("Execute these exact terms");
    await con.until("location.hash==='#record'", 8000, "record screen");
    await con.until("document.querySelector('#record-body table')", 8000, "record table");
    await con.click("Simulate transfer PAID");
    await sleep(800);
    await con.until("document.querySelector('#verify-banner')", 6000, "verify banner");
    const rec = (await con.text("#record-body")).replace(/\s+/g, " ");
    check("record: every term matches, N of N checks passed, PASS banner bound to the verify result", /Every term matches what the person approved/.test(rec) && /(\d+) of \1 checks passed/.test(rec) &&
      await con.ev("document.getElementById('verify-banner').getAttribute('data-verdict')==='PASS' && /PASS/.test(document.getElementById('verify-banner').textContent)"));
    check("record: provider statuses are reported verbatim in the Details", /as reported/.test(await con.text("#record-detail-body")));
    await con.fits("record", "record screen");
    await con.shot("15-record-approved-vs-executed");
    await con.frame("v15-record", "One conversion at the approved quote, then one supplier payment. Approved and executed side by side; code checks the comparable terms; statuses as reported.", 14, "#record-body");

    // ---- 5 lookup after a withheld response (O1 from the balance) ------------------------------------------
    await con.hash("verify");
    await con.click("Withhold the next supplier payment response");
    await con.hash("plan");
    await con.ev(`(function(){var c=[].filter.call(document.querySelectorAll('#plan-body .plan-table tbody tr'),function(x){return x.textContent.indexOf('Acme')>=0})[0];[].filter.call(c.querySelectorAll('button'),function(b){return /approval/.test(b.textContent)})[0].click()})()`);
    await con.until("location.hash==='#approval'", 6000, "approval for Acme");
    const link3 = await con.href("#approval-body article:first-of-type a.button.primary");
    const app3 = await open(withTheme(ORIGIN + link3), "approver-3");
    await app3.size(390, 844, true);
    await app3.until("document.getElementById('terms').children.length>0", 6000, "acme approver");
    await app3.ev("document.getElementById('approve').click()");
    await app3.until("/approved/i.test(document.getElementById('result').textContent+document.getElementById('message').textContent)", 6000, "acme approved");
    await con.hash("approval");
    await con.click("Execute these exact terms");
    await con.hash("verify");
    await con.until("[].some.call(document.querySelectorAll('#lookup-body button'),function(b){return /Look up by request ID/.test(b.textContent)})", 8000, "lookup button");
    await con.shot("16-lookup-not-sure-yet");
    await con.click("Look up by request ID");
    await con.until("/Found by request ID/.test(document.getElementById('lookup-body').textContent) && document.querySelector('#lookup-body li.done:nth-child(3)')", 8000, "found by request id");
    const lk = (await con.text("#lookup-body")).replace(/\s+/g, " ");
    check("lookup by request ID, payout create calls stay at 1, never resent", /Payout create calls for this request ID: 1\./.test(lk) && /Never resent/.test(lk));
    await con.shot("17-lookup-found");
    await con.frame("v16-lookup", "A response was withheld by our test switch. The agent never resends: it looks the payout up by its pre-minted request ID and finds it.", 11, "#lookup-body");

    // ---- 6 verify ----------------------------------------------------------------------------------------------
    await con.click("Load record into the box");
    await con.click("Verify this copy");
    await con.until("document.querySelector('#verify-result .verdict.pass')", 6000, "PASS");
    check("verify: unchanged copy passes", true);
    await con.click("Edit a copy: change an executed value");
    await con.click("Verify this copy");
    await con.until("document.querySelector('#verify-result .verdict.fail')", 6000, "FAIL");
    check("verify: edited copy fails and names a row", /MISMATCH|differs|row/i.test(await con.text("#verify-result")), (await con.text("#verify-result")).replace(/\s+/g, " ").slice(0, 90));
    await con.shot("18-verify-pass-then-fail");
    await con.frame("v17-verify", "Anyone can check an exported copy offline. Edit one value and Verify names the row that changed.", 10, "#verify-result");

    // ---- 7 console features: quote validity, fee row, re-check button, late failure after PAID -----------------
    await con.hash("plan");
    await con.until("document.querySelector('#plan-body .plan-table tbody tr')", 6000, "plan rows again");
    const hasSelect = await con.ev(`(function(){var c=[].filter.call(document.querySelectorAll('#plan-body .plan-table tbody tr'),function(x){return x.textContent.indexOf('Initech')>=0})[0];var s=c&&c.querySelector('select');return !!s && [].map.call(s.options,function(o){return o.value}).join()==='MIN_1,MIN_15,HR_1'})()`);
    check("validity select on a CONVERT card offers MIN_1, MIN_15, HR_1", hasSelect);
    await con.ev(`(function(){var c=[].filter.call(document.querySelectorAll('#plan-body .plan-table tbody tr'),function(x){return x.textContent.indexOf('Initech')>=0})[0];var s=c.querySelector('select');s.value='MIN_15';s.dispatchEvent(new Event('change',{bubbles:true}));[].filter.call(c.querySelectorAll('button'),function(b){return b.textContent.indexOf('locked quote')>=0})[0].click()})()`);
    await con.until("location.hash==='#approval' && document.querySelector('#approval-body article')", 6000, "approval for Initech");
    await sleep(500);
    const secs = (t) => { const m = /(\d\d):(\d\d):(\d\d)/.exec(t || ""); return m ? (+m[1]) * 3600 + (+m[2]) * 60 + (+m[3]) : NaN; };
    const initechTxt = await con.ev(`(function(){var a=[].filter.call(document.querySelectorAll('#approval-body article'),function(x){return x.textContent.indexOf('Initech')>=0}).pop();return a?a.textContent:''})()`);
    const clock = secs(await con.text("#server-clock"));
    const vt = /Quote valid to\s*(\d\d:\d\d:\d\d)/.exec(initechTxt), ax = /Approval closes\s*(\d\d:\d\d:\d\d)/.exec(initechTxt);
    check("fee row shows the payout fee and method", /Payout fee\s*\w+ [\d.]+ \((LOCAL|SWIFT)\)/.test(initechTxt), (/Payout fee[^A-Z]*\([A-Z]+\)/.exec(initechTxt) || [""])[0]);
    const quoteLeft = vt ? secs(vt[1]) - clock : NaN, apprLeft = ax ? secs(ax[1]) - clock : NaN;
    check("MIN_15 quote: valid about 15 minutes and the approval window follows it (over 10 minutes)", quoteLeft > 840 && quoteLeft <= 905 && apprLeft > 700 && apprLeft < quoteLeft,
      `quote left ${quoteLeft}s, approval left ${apprLeft}s`);
    await con.shot("20-approval-15-minute-validity");
    await con.hash("record");
    await con.until("document.getElementById('pick-record')", 6000, "record picker");
    await con.ev(`(function(){var s=document.getElementById('pick-record');var o=[].filter.call(s.options,function(x){return x.textContent.indexOf('Globex')>=0})[0];s.value=o.value;s.dispatchEvent(new Event('change',{bubbles:true}))})()`);
    await con.until("document.querySelector('#record-body table') && /Globex/.test(document.getElementById('record-body').textContent)", 6000, "Globex record");
    check("record shows PAID for Globex with the re-check button", await con.ev(`/PAID/.test(document.getElementById('record-body').textContent) && [].some.call(document.querySelectorAll('#record-body button'),function(b){return /Check payout status again/.test(b.textContent)})`));
    await con.click("Check payout status again");
    await sleep(800);
    check("re-check keeps a PAID transfer PAID", await con.ev(`/Check payout status again/.test(document.getElementById('record-body').textContent) && !/PAYOUT_FAILED/.test(document.getElementById('record-body').textContent)`));
    await con.click("Simulate a late FAILED after PAID");
    await con.until("/PAYOUT_FAILED/.test(document.getElementById('record-body').textContent)", 6000, "late failure shown");
    const lateTxt = (await con.text("#record-body")).replace(/\s+/g, " ");
    check("late failure after PAID is shown as PAYOUT_FAILED, not retried, a person decides", /Payment failed\. Not retried\. A person decides/.test(lateTxt), lateTxt.slice(0, 100));
    check("failed payout has its own red notice apart from the matching terms", await con.ev("!!document.querySelector('#record-body .notice.bad') && /Approved terms match\. The payout failed/.test(document.getElementById('record-body').textContent)"));
    await con.shot("21-record-late-failure-after-paid");

    // ---- phone-width and overflow pass over every screen ----------------------------------------------------------
    // (not under --stage: that is the larger recording type for desktop, not a phone layout)
    if (!STAGE) await con.size(390, 844, true);
    for (const id of ["invoices", "plan", "approval", "stale", "record", "verify"]) {
      await con.hash(id); await sleep(500);
      const over = await con.overflow();
      if (!STAGE) check(`phone width 390: no horizontal page overflow on #${id}`, over <= 1, over > 1 ? `overflow ${over}px` : "");
      if (!STAGE) await con.shot(`phone-${id}`, { min: 844 });
    }
    await con.size(VIDEO ? 1920 : DESK[0], VIDEO ? 886 : DESK[1]);
    for (const id of ["invoices", "plan", "approval", "stale", "record", "verify"]) {
      await con.hash(id);
      const over = await con.overflow();
      check(`desktop ${DESK[0]}: no horizontal page overflow on #${id}`, over <= 1, over > 1 ? `overflow ${over}px` : "");
    }
    await con.ev("document.querySelector('details.calls').open=true");
    await con.hash("verify");
    await con.shot("19-desktop-calls-log", { full: true });
    await con.ev("window.scrollTo(0, document.body.scrollHeight)");
    await con.frame("v18-end", "Rate-Locked Approval. Test money, fictional suppliers, local fake and scripted reader. Independent project, not affiliated with Airwallex.", 8, "footer");
    if (VIDEO) writeFileSync(join(VIDEO, "frames.json"), JSON.stringify(frames, null, 2));
    }   // end of the fake-world story
  } finally {
    for (const p of pages) check(`no page errors on ${p.label}`, p.errors.length === 0, p.errors.slice(0, 2).join(" ; "));
    try { await cdp.send("Browser.close"); } catch { /* closing */ }
    proc.kill();
    if (serverProc) serverProc.kill();
  }
  const bad = results.filter((r) => !r.ok);
  console.log(`\n${results.length - bad.length}/${results.length} checks passed`);
  process.exit(bad.length ? 1 : 0);
}
main().catch((e) => { console.error("QA run failed:", e.message); process.exit(2); });

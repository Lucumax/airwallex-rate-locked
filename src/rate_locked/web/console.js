"use strict";
/* Rate-Locked console. Vanilla JS, no framework, no network beyond this origin. Every value shown comes from
   GET /api/state or an action's answer; nothing is computed or invented here except clock display. */
(function () {
  var CSRF = document.querySelector('meta[name="csrf-token"]').getAttribute("content");
  var PROV = { FAKE: "fake", SANDBOX: "sandbox", FIXTURE: "fixture", CODE: "code", "TEST DOUBLE": "test-double", MODEL: "model", HUMAN: "human" };
  var ZONES = [["+00:00", "UTC+00:00 (what the reader assumed)"], ["+01:00", "UTC+01:00"], ["+08:00", "UTC+08:00"], ["-05:00", "UTC-05:00"]];
  var SCREENS = ["invoices", "plan", "approval", "stale", "record", "verify"];
  var GLYPH = { FUND: "✓", CONVERT: "⇄", DEFER: "◷", ESCALATE: "▲" };
  var FIELD_NAMES = { supplier: "Supplier", invoice_number: "Invoice number", amount: "Amount", currency: "Currency", due_date: "Due date" };
  var state = null, lastKey = "", fetchedAt = 0, picked = null, records = {}, busy = false;

  var params = new URLSearchParams(location.search);
  if (params.get("stage") === "video") document.body.classList.add("stage");
  var showAll = params.get("all") === "1";

  // ---- DOM helpers (text nodes only; never HTML strings) ------------------------------------------
  function add(n, c) {
    if (c === null || c === undefined || c === false) return;
    if (Array.isArray(c)) { c.forEach(function (x) { add(n, x); }); return; }
    n.appendChild(typeof c === "string" || typeof c === "number" ? document.createTextNode(String(c)) : c);
  }
  function el(tag, attrs) {
    var n = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      var v = attrs[k];
      if (v === null || v === undefined || v === false) return;
      if (k === "on") Object.keys(v).forEach(function (ev) { n.addEventListener(ev, v[ev]); });
      else if (k === "class") n.className = v;
      else n.setAttribute(k, v === true ? "" : String(v));
    });
    for (var i = 2; i < arguments.length; i++) add(n, arguments[i]);
    return n;
  }
  function fill(id) {
    var n = document.getElementById(id);
    while (n.firstChild) n.removeChild(n.firstChild);
    for (var i = 1; i < arguments.length; i++) add(n, arguments[i]);
  }
  function srcTag(label) { return el("b", { "data-provenance": PROV[label] || "code" }, label); }
  function source() {
    var p = el("p", { class: "source" });
    for (var i = 0; i < arguments.length; i++) add(p, arguments[i]);
    return p;
  }
  function rows(pairs) {
    var dl = el("dl", { class: "rows" });
    pairs.forEach(function (p) { if (p) { add(dl, el("dt", null, p[0])); add(dl, el("dd", { class: p[2] || null }, p[1])); } });
    return dl;
  }
  function chip(cls, text) { return el("span", { class: "chip " + (cls || "") }, text); }
  var STATE_LABEL = { AWAITING_APPROVAL: ["Awaiting approval", "warn"], APPROVED: ["Approved", "ok"], PAID: ["Paid", "ok"],
    PAYOUT_REPORTED: ["Payout reported", "info"], PAYOUT_FAILED: ["Payment failed", "bad"], CONVERSION_FAILED: ["Conversion failed", "bad"],
    EXPIRED: ["Expired", "bad"], VOIDED: ["Voided", "bad"], DENIED: ["Denied", "bad"], UNKNOWN: ["Outcome not known", "warn"],
    INDETERMINATE: ["Outcome not known", "warn"] };
  function stateChip(code) {
    var l = STATE_LABEL[code] || [code, ""];
    return el("span", { class: "chip status " + l[1], title: code }, l[0]);
  }
  function hms(iso) {
    if (!iso) return "-";
    var d = new Date(iso);
    function p(x) { return (x < 10 ? "0" : "") + x; }
    return p(d.getUTCHours()) + ":" + p(d.getUTCMinutes()) + ":" + p(d.getUTCSeconds()) + " UTC";
  }
  function when(iso) {
    var d = new Date(iso);
    var mon = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"][d.getUTCMonth()];
    return mon + " " + d.getUTCDate() + ", " + hms(iso).slice(0, 5) + " UTC";
  }
  function digestNode(d) {
    return el("span", { class: "mono" }, el("strong", null, d.slice(0, 8)), d.slice(8, 24) + "...");
  }

  // ---- talking to the server ------------------------------------------------------------------------
  function api(path, body) {
    var opts = { method: body === undefined ? "GET" : "POST", credentials: "same-origin",
                 headers: { "X-CSRF-Token": CSRF } };
    if (body !== undefined) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
    return fetch(path, opts).then(function (r) {
      return r.json().then(function (j) {
        if (!r.ok) { var e = new Error(j.message || j.error || "request failed"); e.code = j.error; throw e; }
        return j;
      });
    });
  }
  function say(text) { document.getElementById("status").textContent = text; }
  function showError(e) {
    var n = document.getElementById("error");
    n.textContent = e ? (e.message || String(e)) : "";
    n.hidden = !e;
  }
  function act(path, body, done) {
    if (busy) return;
    busy = true;
    document.body.setAttribute("aria-busy", "true");
    showError(null);
    api(path, body).then(function (out) {
      if (done) done(out);
      return refresh(true);
    }).catch(showError).then(function () {
      busy = false;
      document.body.removeAttribute("aria-busy");
    });
  }
  function refresh(force) {
    return api("/api/state").then(function (s) {
      state = s;
      fetchedAt = performance.now();
      var copy = Object.assign({}, s);
      delete copy.server_now;
      var key = JSON.stringify(copy);
      if (force || key !== lastKey) { lastKey = key; render(); }
      tick();
    }).catch(showError);
  }

  // ---- routing --------------------------------------------------------------------------------------
  var SCREEN_STEP = { invoices: 1, plan: 2, approval: 3, stale: 4, record: 5, verify: 5 };
  function route() {
    var id = (location.hash || "#invoices").slice(1);
    if (SCREENS.indexOf(id) < 0) id = "invoices";
    SCREENS.forEach(function (s) { document.getElementById(s).hidden = !showAll && s !== id; });
    document.querySelectorAll(".steps li").forEach(function (li) {
      var screens = li.getAttribute("data-screens").split(" "), a = li.querySelector("a");
      if (screens.indexOf(id) >= 0) a.setAttribute("aria-current", "step"); else a.removeAttribute("aria-current");
      li.className = SCREEN_STEP[screens[0]] < SCREEN_STEP[id] ? "done" : "";
    });
    document.getElementById("chip-reader").hidden = id !== "invoices";  // the reader chip belongs to the invoices screen only
  }
  function focusHeading() {
    var id = (location.hash || "#invoices").slice(1);
    var h = document.getElementById("h-" + id);
    if (h) h.focus({ preventScroll: true });
  }
  window.addEventListener("hashchange", function () {
    route(); focusHeading(); if (state) guide();
    window.scrollTo(0, 0);  // a new screen starts at the top: the world chip and the stepper stay in view
    if (window.requestAnimationFrame) window.requestAnimationFrame(function () { window.scrollTo(0, 0); });
  });
  function go(id) { location.hash = "#" + id; }

  // ---- chips -----------------------------------------------------------------------------------------
  function chips() {
    var s = state;
    function set(id, text, prov) {
      var n = document.getElementById(id);
      n.textContent = text;
      if (prov) n.setAttribute("data-provenance", prov);
    }
    set("chip-world", "WORLD: " + s.chips.world, PROV[s.sources.balances]);
    set("chip-money", s.chips.money);
    set("chip-run", "RUN: " + s.chips.run, "code");
    set("chip-reader", "READER: " + s.chips.reader, PROV[s.sources.reader]);
    document.querySelectorAll(".src-world").forEach(function (n) {
      n.textContent = s.sources.balances;
      n.setAttribute("data-provenance", PROV[s.sources.balances]);
    });
    var r = document.getElementById("src-reader");
    r.textContent = s.sources.reader;
    r.setAttribute("data-provenance", PROV[s.sources.reader]);
    document.getElementById("reader-note").textContent = s.sources.reader === "MODEL"
      ? "A model reads; code decides."
      : "The reader here is a scripted test double, not a model; code decides.";
    document.getElementById("about-world").textContent = s.world === "FAKE"
      ? "This run is the local FAKE of the Airwallex endpoints. Nothing here calls Airwallex or any model."
      : "This run talks to the Airwallex sandbox with test money. Invoices and receipts are fictional fixtures; the reader is labelled above.";
    if (s.world !== "FAKE") {
      document.getElementById("world-line").textContent = "Every number on these screens comes from this run. The world is the Airwallex sandbox (test money): balances, rates, quotes, conversions and transfers come from its API.";
    }
  }

  // ---- H0 invoices -------------------------------------------------------------------------------------
  function marked(text, fields) {
    var ranges = fields.filter(function (f) { return f.start !== null; })
      .map(function (f) { return [f.start, f.end]; }).sort(function (a, b) { return a[0] - b[0]; });
    var merged = [];
    ranges.forEach(function (r) {
      var last = merged[merged.length - 1];
      if (last && r[0] <= last[1]) last[1] = Math.max(last[1], r[1]); else merged.push([r[0], r[1]]);
    });
    var pre = el("pre", { class: "doc", "data-provenance": "fixture" });
    var at = 0;
    merged.forEach(function (r) {
      add(pre, text.slice(at, r[0]));
      add(pre, el("mark", null, text.slice(r[0], r[1])));
      at = r[1];
    });
    add(pre, text.slice(at));
    return pre;
  }
  function fieldOf(inv, name) { return inv.fields.filter(function (f) { return f.name === name; })[0]; }
  function checkCell(f) {
    if (f.status === "OK") return el("td", { "data-provenance": "code" }, chip("ok", "✓ OK"));
    if (f.status === "RESOLVED") {
      return el("td", { "data-provenance": "human" }, el("span", { class: "chip", "data-provenance": "human" }, "✓ Confirmed by a person"),
        el("span", { class: "muted" }, " was " + f.resolution.flags_before.join(", ")));
    }
    return el("td", { "data-provenance": "code" }, chip("bad", "▲ Review: " + f.flags.join(", ").replace(/_/g, "_​")));
  }
  function reviewForm(inv) {
    var flagged = inv.fields.filter(function (f) { return f.status === "REVIEW"; });
    var resolved = inv.fields.filter(function (f) { return f.status === "RESOLVED"; });
    if (!flagged.length && !resolved.length) return null;
    var parts = [el("h4", null, inv.review.status === "PENDING" ? "Review needed before planning" : "Review resolved by a person")];
    flagged.forEach(function (f) {
      var tz = f.name === "due_date" && f.flags.indexOf("TIMEZONE_NOT_STATED") >= 0 && f.local && !inv.flags.length;
      if (!tz) { parts.push(el("p", { class: "muted" }, "This flag cannot be answered here (" + f.flags.join(", ") + "). Read the invoice again.")); return; }
      parts.push(el("p", null, "The reader assumed UTC and code does not accept an assumption, so this invoice is held and the plan waits. Choose the time zone the supplier meant; the date and time stay as written."));
      var zone = el("select", { id: "rv-zone-" + inv.invoice_id, required: true },
        el("option", { value: "" }, "Choose a time zone"),
        ZONES.map(function (z) { return el("option", { value: z[0] }, z[1]); }));
      var note = el("input", { type: "text", id: "rv-note-" + inv.invoice_id, maxlength: "200", autocomplete: "off",
        placeholder: "How you checked it" });
      parts.push(el("div", { class: "review-form" },
        el("label", { for: zone.id }, "Time zone for " + f.local.replace("T", " ")), zone,
        el("label", { for: note.id }, "Note (required, kept in the run)"), note,
        el("div", { class: "row-actions" }, el("button", { type: "button", class: "primary", on: { click: function () {
          if (!zone.value) { showError(new Error("Choose a time zone first.")); return; }
          act("/api/review/resolve", { invoice_id: inv.invoice_id, field: f.name, value: f.local + ":00" + zone.value, note: note.value },
            function (out) { say(out.plan_version ? "Review resolved. Plan v" + out.plan_version + " built." : "Review resolved."); });
        } } }, "Confirm deadline"))));
    });
    resolved.forEach(function (f) {
      var r = f.resolution;
      parts.push(el("p", { "data-provenance": "human" }, el("span", { class: "chip", "data-provenance": "human" }, "HUMAN"), " ",
        FIELD_NAMES[f.name] + " confirmed by a person (" + r.by + ") at " + hms(r.at) + ": ",
        el("strong", null, f.name === "due_date" ? when(r.value) : r.value), ". Note: " + r.note));
    });
    return el("div", { class: "review" + (inv.review.status === "PENDING" ? " open" : ""), "data-provenance": "human" }, parts);
  }
  function heldReasons(inv) {
    var out = [];
    inv.fields.forEach(function (f) {
      if (f.status !== "REVIEW") return;
      if (f.flags.indexOf("TIMEZONE_NOT_STATED") >= 0) out.push("Due time has no time zone");
      else out.push((FIELD_NAMES[f.name] || f.name) + " needs review: " + f.flags.join(", ").replace(/_/g, "_​"));
    });
    if (!out.length && inv.flags.length) out.push("Reader reply problems: " + inv.flags.join(", "));
    return out;
  }
  var pickedInv = null;
  function groupState(inv, names) {
    var st = names.map(function (n) { var f = fieldOf(inv, n); return f ? f.status : "REVIEW"; });
    return st.indexOf("REVIEW") >= 0 ? "review" : (st.indexOf("RESOLVED") >= 0 ? "person" : "ok");
  }
  function inspector(inv) {
    var num = fieldOf(inv, "invoice_number"), due = fieldOf(inv, "due_date");
    var flagged = inv.fields.filter(function (f) { return f.status === "REVIEW"; });
    var look = flagged[0] || due;
    var groups = [["Supplier and invoice number", ["supplier", "invoice_number"]], ["Amount and currency", ["amount", "currency"]], ["Due date", ["due_date"]]];
    var list = el("ul", { class: "checks" }, groups.map(function (g) {
      var s = groupState(inv, g[1]);
      return el("li", { class: "st-" + s }, el("span", { class: "mark", "aria-hidden": "true" }, s === "review" ? "▲" : "✓"), " " + g[0],
        el("span", { class: "sr-only" }, s === "review" ? ": needs review" : (s === "person" ? ": confirmed by a person" : ": checked")));
    }));
    var why = null;
    if (flagged.length) {
      var f0 = flagged[0];
      why = el("p", { class: "why-stopped" }, el("strong", null, "Why it stopped: "),
        f0.flags.indexOf("TIMEZONE_NOT_STATED") >= 0 ? "No time zone specified in the source." : "Flags " + f0.flags.join(", ").replace(/_/g, "_​") + ".");
    } else {
      why = el("p", { class: "muted small" }, "Code found these exact words in the invoice and checked the value against them.");
    }
    var viewBtn = el("button", { type: "button", class: "linkbtn", on: { click: function () {
      var d = document.getElementById("inv-details");
      d.open = true;
      var t = document.getElementById("inv-" + inv.invoice_id);
      if (t) t.scrollIntoView({ block: "start" });
    } } }, "View source invoice");
    return [el("h3", null, "What the reader checked"), list,
      el("p", { class: "label" }, "Source · " + ((num && num.value) || inv.invoice_id)),
      look && look.quote ? el("p", { class: "srcquote mono", "data-provenance": "fixture" }, "“" + look.quote + "”") : null,
      why, reviewForm(inv), el("p", null, viewBtn)];
  }
  function plural(n, one, many) { return n + " " + (n === 1 ? one : many); }
  function invoices() {
    var ordered = state.invoices.slice().sort(function (a, b) {
      return Number(b.review.status === "PENDING") - Number(a.review.status === "PENDING");
    });
    var nReady = ordered.filter(function (i) { return i.ready; }).length, nHeld = ordered.length - nReady;
    document.getElementById("invoices-sub").textContent = plural(ordered.length, "invoice", "invoices") + " read. " + nReady + " ready to plan." +
      (nHeld ? " " + nHeld + (nHeld === 1 ? " needs" : " need") + " a closer look." : "");
    if (!pickedInv || !ordered.some(function (i) { return i.invoice_id === pickedInv; })) {
      pickedInv = (ordered.filter(function (i) { return i.review.status === "PENDING"; })[0] || ordered.filter(function (i) { return !i.ready; })[0] || ordered[0]).invoice_id;
    }
    var holder = el("aside", { class: "inspector panel", id: "inspector", "aria-label": "What the reader checked" });
    function select(id) {
      pickedInv = id;
      document.querySelectorAll("#invoices-body .rowbtn").forEach(function (b) { b.setAttribute("aria-pressed", String(b.getAttribute("data-inv") === id)); });
      document.querySelectorAll("#invoices-body tr.inv-row").forEach(function (r) { r.className = "inv-row" + (r.getAttribute("data-inv") === id ? " sel" : ""); });
      fill("inspector", inspector(ordered.filter(function (i) { return i.invoice_id === id; })[0]));
    }
    var tb = el("tbody");
    ordered.forEach(function (inv) {
      var sup = fieldOf(inv, "supplier"), num = fieldOf(inv, "invoice_number"), amt = fieldOf(inv, "amount"),
        cur = fieldOf(inv, "currency"), due = fieldOf(inv, "due_date");
      var warn = inv.ready ? [] : heldReasons(inv);
      var dueHeld = due && due.status === "REVIEW";
      add(tb, el("tr", { class: "inv-row" + (inv.invoice_id === pickedInv ? " sel" : ""), "data-inv": inv.invoice_id },
        el("td", null, el("button", { type: "button", class: "rowbtn", "data-inv": inv.invoice_id, "aria-pressed": String(inv.invoice_id === pickedInv),
          on: { click: function () { select(inv.invoice_id); } } },
          el("span", { class: "nm" }, (sup && sup.value) || inv.invoice_id),
          el("span", { class: "ref" }, (num && num.value) || ""),
          warn.map(function (w) { return el("span", { class: "warnline" }, "▲ " + w); }))),
        el("td", { class: "num strong" }, (cur && cur.value ? cur.value + " " : "") + (amt && amt.value !== null ? amt.value : "-")),
        el("td", { class: dueHeld ? "warn" : "num" }, dueHeld ? "Needs clarification" : (due && due.value ? when(due.value) : "Missing")),
        el("td", null, inv.ready ? chip("ok status-chip", "✓ READY") : chip("warn status-chip", "NEEDS REVIEW"),
          inv.review.status === "RESOLVED" ? el("span", { class: "small muted block" }, "Confirmed by a person") : null)));
    });
    var table = el("div", { class: "panel list" },
      el("div", { class: "panel-head" }, el("h3", null, "Supplier invoices"), el("span", { class: "muted" }, plural(ordered.length, "invoice", "invoices"))),
      el("div", { class: "table-wrap" }, el("table", { class: "inv-table" },
        el("thead", null, el("tr", null, el("th", null, "Supplier"), el("th", null, "Amount"), el("th", null, "Due"), el("th", null, "Status"))), tb)),
      el("p", { class: "panel-foot muted small" }, "Read from " + plural(ordered.length, "source invoice", "source invoices") + " (fictional)"));
    var planBtn = state.plans.length && !state.review.pending.length
      ? el("a", { class: "button primary", href: "#plan" }, "Plan payments →")
      : el("button", { type: "button", class: "primary", disabled: true, "aria-describedby": "plan-hold" }, "Plan payments →");
    var bar = el("div", { class: "actionbar" },
      el("p", { class: "assure" }, el("span", { class: "ico-shield", "aria-hidden": "true" }, "◈"), " Every decision is recorded with its source."),
      el("div", { class: "go" }, planBtn,
        el("p", { class: "small muted", id: "plan-hold" }, plural(nReady, "ready invoice", "ready invoices") + " · " + nHeld + " held for review" +
          (state.review.pending.length ? ". The plan waits for the review: nothing is planned, quoted, converted or paid until then." : ""))));
    fill("invoices-body", el("div", { class: "two-col" }, table, holder), bar);
    fill("inspector", inspector(ordered.filter(function (i) { return i.invoice_id === pickedInv; })[0]));
    // details: the full text of each invoice, every field with its verbatim source words and offsets
    fill("invoices-detail-body", ordered.map(function (inv) {
      var sup = fieldOf(inv, "supplier");
      var tbody = el("tbody");
      inv.fields.forEach(function (f) {
        add(tbody, el("tr", null,
          el("th", { scope: "row" }, FIELD_NAMES[f.name] || f.name),
          el("td", { class: "num", "data-provenance": f.status === "RESOLVED" ? "human" : "test-double" }, f.value === null ? "-" : (f.name === "due_date" ? when(f.value) : f.value)),
          el("td", { class: "mono", "data-provenance": "fixture" }, f.quote === null ? "-" : "“" + f.quote + "”"),
          el("td", { class: "num mono", "data-provenance": "code" }, f.start === null ? "-" : f.start + " to " + f.end),
          checkCell(f)));
      });
      var resolvedNote = inv.review.status === "RESOLVED" ? " (confirmed by a person)" : "";
      return el("article", { class: "panel", id: "inv-" + inv.invoice_id, "aria-label": inv.invoice_id },
        el("h3", null, (sup && sup.value) || inv.invoice_id, " ",
          inv.ready ? chip("ok", "✓ Ready for planning as " + inv.obligation_id + resolvedNote) : chip("bad", "▲ Needs review: held from the plan")),
        inv.flags.length ? el("p", { class: "bad" }, "Reply problems: " + inv.flags.join(", ")) : null,
        inv.review.status ? source("Source: flag raised by ", srcTag("CODE"), "; answer given by a ", srcTag("HUMAN"), " on this console and kept in the run.") : null,
        el("div", { class: "grid2 inv" },
          el("div", null, el("p", { class: "muted" }, "Invoice text (fictional). Highlighted: the words the reader quoted."), marked(inv.text, inv.fields)),
          el("div", { class: "table-wrap" }, el("table", null,
            el("thead", null, el("tr", null, el("th", null, "Field"), el("th", { class: "num" }, "Value read"),
              el("th", null, "Source words (verbatim)"), el("th", { class: "num" }, "Offsets"), el("th", null, "Check by code"))),
            tbody))));
    }));
  }
  function waitingText() {
    var refs = state.review.pending.map(function (id) {
      var inv = state.invoices.filter(function (i) { return i.invoice_id === id; })[0];
      var num = inv && fieldOf(inv, "invoice_number"), sup = inv && fieldOf(inv, "supplier");
      return (num && num.value ? num.value : id) + (sup && sup.value ? " (" + sup.value + ")" : "");
    });
    return "The plan waits: " + refs.join(", ") + " needs a person's review on the Invoices screen. Until then nothing is planned, quoted, converted or paid.";
  }

  // ---- H1 plan ------------------------------------------------------------------------------------------
  function reserveTable(res) {
    var tb = el("tbody");
    res.rows.forEach(function (r) {
      add(tb, el("tr", null, el("th", { scope: "row" }, r.currency), el("td", { class: "num" }, r.available),
        el("td", { class: "num" }, r.reserved), el("td", { class: "num" }, el("strong", null, r.remaining))));
    });
    return el("div", { class: "table-wrap", "data-provenance": "code" }, el("table", null,
      el("thead", null, el("tr", null, el("th", null, "Currency"), el("th", { class: "num" }, "Available"),
        el("th", { class: "num" }, "Plan relies on"), el("th", { class: "num" }, "Remaining reserve"))), tb),
      el("p", { class: "muted" }, res.note, " Balances read at ", hms(res.read_at), ".",
        res.excluded.length ? " Already executed, not counted again: " + res.excluded.join(", ") + "." : ""));
  }
  function planInputs() {
    var s = state, w = s.sources.balances;
    var bal = el("div", { class: "panel", "data-provenance": PROV[w] }, el("h3", null, "Balances"),
      source("Source: ", srcTag(w), " GET /balances/current, read at ", hms(s.balances.read_at)),
      rows(s.balances.rows.map(function (r) { return [r.currency, r.available, "hero-num"]; })),
      s.balances.note ? el("p", { class: "muted" }, s.balances.note) : null);
    var rates = el("div", { class: "panel", "data-provenance": PROV[w] }, el("h3", null, "Indicative rates"),
      source("Source: ", srcTag(w), " GET /fx/rates/current. Indicative only: approvals bind to a locked quote, never to these."),
      rows(s.rates.map(function (r) { var p = r.pair.split("/"); return ["1 " + p[1] + " costs", p[0] + " " + r.sell_per_buy]; })));
    var rec = el("div", { class: "panel", "data-provenance": "fixture" }, el("h3", null, "Expected receipts"),
      source("Source: ", srcTag("FIXTURE"), " (fictional payer, confidence declared by the fixture)"),
      s.receipts.map(function (r) {
        return el("p", null, r.receipt_id + " " + r.payer + ": " + r.currency + " " + r.amount + " expected " + when(r.expected_at) + " ",
          chip(r.confidence === "HIGH" ? "ok" : "warn", r.confidence + " confidence"));
      }));
    var pol = el("div", { class: "panel", "data-provenance": "code" }, el("h3", null, "Execution limit"),
      source("Source: ", srcTag("CODE"), " ", s.policy.label),
      rows([["High forecast confidence", s.policy.base + " " + s.policy.ceiling_high],
            ["Otherwise", s.policy.base + " " + s.policy.ceiling_low],
            ["Planning window", s.policy.horizon_hours + " hours"],
            s.policy.planning_pinned ? ["Planning time (pinned)", when(s.policy.planning_now) + ": fixture time, so the fictional due dates stay in the window. Quotes and approvals use the real clock."] : null,
            ["Estimate buffer", (s.policy.buffer_bps / 100).toFixed(1) + "%"]]),
      el("p", { class: "muted" }, "The limit only decides whether a conversion is escalated to a person. Every payment still needs a person to approve the exact terms."));
    var res = el("div", { class: "panel" }, el("h3", null, "Remaining reserve"),
      source("Source: ", srcTag("CODE"), " from ", srcTag(w), " balances and plan v" + s.reserve.plan_version),
      reserveTable(s.reserve));
    fill("plan-inputs", el("div", { class: "grid2" }, bal, rates, rec, pol), res);
  }
  var validityChoice = {};
  var VALIDITY = [["MIN_1", "1 minute (default)"], ["MIN_15", "15 minutes"], ["HR_1", "1 hour"]];
  function liveRequest(oid) {
    var mine = state.executions.filter(function (e) { return e.obligation_id === oid; });
    var last = mine[mine.length - 1];
    if (!last || ["DENIED", "EXPIRED", "VOIDED", "CONVERSION_FAILED", "PAYOUT_FAILED"].indexOf(last.state) >= 0) return null;
    return last;
  }
  // a read-only card for a plan kept in the Details (the actions live on the decision table)
  function card(plan, d, prev) {
    var reused = plan.reused.indexOf(d.obligation_id) >= 0;
    var changed = plan.changed.indexOf(d.obligation_id) >= 0;
    var status = plan.version === 1 ? null : el("span", { class: "recalc" },
      reused ? "Not recomputed (its inputs did not change)" : (changed ? "Recomputed" : "Recomputed, same result"));
    var was = null;
    if (changed && prev) {
      var before = prev.decisions.filter(function (x) { return x.obligation_id === d.obligation_id; })[0];
      if (before) was = el("p", { class: "was" }, "Changed from ", el("strong", null, before.action), " to ", el("strong", null, d.action), ".");
    }
    return el("article", { class: "card" + (reused ? " reused" : "") + (changed ? " changed" : ""), "aria-label": d.supplier, "data-provenance": "code" },
      el("header", null, el("h3", null, d.obligation_id + " " + d.supplier), el("span", { class: "ref" }, d.invoice_ref + " · due " + when(d.due_at))),
      el("p", { class: "amount" }, el("span", { class: "muted" }, d.currency + " "), d.amount),
      el("p", null, chip(d.action.toLowerCase(), GLYPH[d.action] + " " + d.action), " ", status),
      el("p", { class: "why" }, d.reason_text),
      rows([["Plan", d.uses], ["Approval", d.approval],
            d.ceiling_amount ? ["Limit", state.policy.base + " " + d.ceiling_amount + " (" + d.ceiling_level.toLowerCase() + " forecast confidence)"] : null]),
      was);
  }
  function planCol(plan, prev, isLatest) {
    var marks = plan.diff.map(function (x) {
      return [el("li", { class: "minus" }, "- " + x.field + ": " + x.before), el("li", { class: "plus" }, "+ " + x.field + ": " + x.after)];
    });
    return el("div", { class: "plan-col panel" },
      el("h3", null, plan.title, isLatest ? " (current)" : ""),
      el("p", { class: "muted" }, "Plan digest ", el("span", { class: "mono" }, plan.digest.slice(0, 16) + "..."),
        " · limit " + plan.ceiling_amount + " (" + plan.ceiling.toLowerCase() + " forecast confidence)"),
      el("ul", { class: "diff" }, plan.trigger.map(function (t) { return el("li", null, (plan.version === 1 ? "= " : "Trigger: ") + t); })),
      marks.length ? el("ul", { class: "diff", "aria-label": "What changed in the inputs" }, marks) : null,
      plan.version > 1 ? el("p", { class: "muted" }, "Recomputed " + plan.recomputed.length + " of " + plan.decisions.length +
        " obligations; " + plan.reused.length + " left as they were.") : null,
      el("div", { class: "grid3" }, plan.decisions.map(function (d) { return card(plan, d, prev); })));
  }
  // one sentence built from the decisions (code), not from the reader: what the plan recommends
  function recommendation(plan) {
    var parts = plan.decisions.map(function (d) {
      if (d.action === "FUND") return "pay " + d.supplier + " from the " + d.currency + " balance";
      if (d.action === "CONVERT") return "convert to " + d.currency + " and pay " + d.supplier;
      if (d.action === "DEFER") return "defer " + d.supplier;
      return "send " + d.supplier + " to a person";
    });
    var s = parts.length > 1 ? parts.slice(0, -1).join(", ") + " and " + parts[parts.length - 1] : (parts[0] || "");
    s = s.charAt(0).toUpperCase() + s.slice(1);
    return /\.$/.test(s) ? s : s + ".";
  }
  function planRow(plan, d, prev, primaryOid) {
    var reused = plan.reused.indexOf(d.obligation_id) >= 0;
    var changed = plan.changed.indexOf(d.obligation_id) >= 0;
    var status = plan.version === 1 ? null : el("span", { class: "recalc" },
      reused ? "Not recomputed (its inputs did not change)" : (changed ? "Recomputed" : "Recomputed, same result"));
    var was = null;
    if (changed && prev) {
      var before = prev.decisions.filter(function (x) { return x.obligation_id === d.obligation_id; })[0];
      if (before) was = el("span", { class: "was" }, "Changed from ", el("strong", null, before.action), " to ", el("strong", null, d.action), ".");
    }
    var action = null;
    if (d.executable) {
      var live = liveRequest(d.obligation_id);
      action = live ? el("span", { class: "small muted" }, "Request open: " + live.state_text + " ", el("a", { href: "#approval" }, "See approval"))
        : el("div", { class: "rowact" },
            d.action === "CONVERT" ? el("label", { class: "muted small" }, "Quote valid for ", el("select", { "aria-label": "How long the locked quote stays valid", on: { change: function (ev) { validityChoice[d.obligation_id] = ev.target.value; } } },
              VALIDITY.map(function (o) {
                return el("option", { value: o[0], selected: (validityChoice[d.obligation_id] || "MIN_1") === o[0] }, o[1]);
              }))) : null,
            el("button", { type: "button", class: d.obligation_id === primaryOid ? "primary" : "", on: { click: function () {
              act("/api/prepare", { obligation_id: d.obligation_id, validity: validityChoice[d.obligation_id] || "MIN_1" }, function () { go("approval"); });
            } } }, d.action === "CONVERT" ? "Get a locked quote and request approval" : "Request approval"));
    }
    return el("tr", { class: reused ? "reused" : (changed ? "changed" : null), "data-provenance": "code", "aria-label": d.supplier },
      el("td", null, el("span", { class: "nm" }, d.obligation_id + " " + d.supplier), el("span", { class: "ref" }, d.invoice_ref)),
      el("td", { class: "num" }, when(d.due_at)),
      el("td", { class: "num strong" }, d.currency + " " + d.amount),
      el("td", null, chip(d.action.toLowerCase(), GLYPH[d.action] + " " + d.action), " ", status,
        el("span", { class: "why" }, d.reason_text), was,
        el("span", { class: "small muted block" }, d.approval + (d.ceiling_amount ? " · limit " + state.policy.base + " " + d.ceiling_amount : ""))),
      el("td", { class: "act" }, action));
  }
  function plan() {
    var lowerBtn = document.getElementById("btn-lower"), depositBtn = document.getElementById("btn-deposit");
    if (!state.plans.length) {
      lowerBtn.disabled = true;
      depositBtn.disabled = true;
      fill("plan-inputs");
      fill("plan-details-body");
      fill("plan-body", el("p", { class: "notice warn" }, waitingText()),
        el("div", { class: "row-actions" }, el("a", { class: "button primary", href: "#invoices" }, "Go to the review")));
      return;
    }
    depositBtn.disabled = false;
    planInputs();
    var plans = state.plans, n = plans.length, latest = plans[n - 1], prev = n > 1 ? plans[n - 2] : null;
    var r1 = state.receipts.filter(function (r) { return r.receipt_id === "R1"; })[0];
    lowerBtn.disabled = !r1 || r1.confidence === "LOW";
    var w = state.sources.balances;
    // the one primary action: the first conversion that still needs a request, else the first payment
    var open = latest.decisions.filter(function (d) { return d.executable && !liveRequest(d.obligation_id); });
    var first = open.filter(function (d) { return d.action === "CONVERT"; })[0] || open[0];
    var banner = el("div", { class: "banner", "data-provenance": "code" },
      el("span", { class: "spark", "aria-hidden": "true" }, "✦"), el("strong", null, "Agent recommendation"),
      el("span", { class: "rec-text" }, recommendation(latest)));
    var strip = el("div", { class: "balances", "data-provenance": PROV[w], role: "group", "aria-label": "Balances" },
      state.balances.rows.map(function (r) {
        return el("div", { class: "bal" }, el("span", { class: "cur" }, r.currency), el("span", { class: "big num" }, r.available), el("span", { class: "muted small" }, "Available"));
      }));
    var rateline = el("div", { class: "rateline", "data-provenance": PROV[w] },
      el("span", { class: "dot", "aria-hidden": "true" }), el("strong", null, w === "SANDBOX" ? "Live Airwallex rate" : "Indicative rate (" + w + " world)"),
      state.rates.map(function (r) { var p = r.pair.split("/"); return el("span", { class: "num" }, "1 " + p[1] + " costs " + p[0] + " " + r.sell_per_buy); }),
      el("span", { class: "muted" }, "Balances read at " + hms(state.balances.read_at)),
      el("span", { class: "grow muted right" }, "Approvals bind to a locked quote, never to this rate"));
    var nConv = latest.decisions.filter(function (d) { return d.action === "CONVERT"; }).length;
    var table = el("div", { class: "panel list" },
      el("div", { class: "panel-head" }, el("h3", null, "Proposed actions (" + latest.title + ")"),
        el("span", { class: "muted" }, plural(latest.decisions.length, "obligation", "obligations"))),
      el("div", { class: "table-wrap" }, el("table", { class: "plan-table" },
        el("thead", null, el("tr", null, el("th", null, "Supplier / invoice"), el("th", null, "Due"), el("th", null, "Amount"), el("th", null, "Decision"), el("th", null, "Next"))),
        el("tbody", null, latest.decisions.map(function (d) { return planRow(latest, d, prev, first && first.obligation_id); })))));
    var reserve = el("div", { class: "reservebar", "data-provenance": "code" },
      el("span", { class: "muted" }, "Remaining reserve"),
      state.reserve.rows.map(function (r) { return el("span", { class: "num" }, r.currency + " ", el("strong", null, r.remaining)); }),
      el("span", { class: "grow right" }, plural(nConv, "conversion needs", "conversions need") + " approval"));
    var bar = el("div", { class: "actionbar" },
      el("p", { class: "assure" }, el("span", { class: "ico-shield", "aria-hidden": "true" }, "◈"), el("strong", null, " Approval is tied to a locked FX quote. "),
        el("span", { class: "muted" }, "If the quote has expired, nothing is sent (zero execution API calls) and the agent asks again with the cost difference.")));
    fill("plan-body", banner, strip, state.balances.note ? el("p", { class: "small muted" }, state.balances.note) : null, rateline, table, reserve, bar);
    // details: the plan's own record, the reasons in a sentence, history and the previous plan
    var ex = state.explanation;
    var expl = el("div", { class: "panel", "data-provenance": PROV[state.sources.reader] }, el("h3", null, "Plain-language explanation"),
      source("Written by the ", srcTag(state.sources.reader), " reader from the structured plan facts; every number checked against them by ", srcTag("CODE"), "."),
      ex.accepted ? el("p", null, ex.text) : el("p", { class: "bad" }, "Explanation rejected: " + ex.problems.join(", ") + ". The reasons on the rows stand."));
    var hist = el("div", { class: "panel" }, el("h3", null, "Plan history"),
      el("ol", null, plans.map(function (p) { return el("li", null, p.title + " · ", el("span", { class: "mono" }, p.digest.slice(0, 16) + "...")); })));
    var prevCol = prev ? planCol(prev, n > 2 ? plans[n - 3] : null, false) : null;
    fill("plan-details-body", planCol(latest, prev, false), expl, hist, prevCol);
  }

  // ---- H2 approval -----------------------------------------------------------------------------------------
  function controlsPanel(buttons) {
    if (!state.controls) return null;
    return el("div", { class: "panel", "data-provenance": "fake" }, el("h3", null, "Test controls (FAKE world only)"),
      el("p", { class: "muted" }, "Each use is logged in this run. These controls do not exist when the world is the sandbox."),
      el("div", { class: "row-actions" }, buttons));
  }
  function execPanel(e) {
    var q = e.quote;
    var actions = el("div", { class: "row-actions" });
    if (e.state === "AWAITING_APPROVAL" && e.approver_path) {
      add(actions, el("a", { class: "button primary", href: e.approver_path, target: "_blank", rel: "noopener noreferrer" }, "Open the approver page"));
    }
    if (e.state === "APPROVED") {
      add(actions, el("button", { type: "button", class: "primary", on: { click: function () {
        act("/api/dispatch", { execution_id: e.execution_id }, function () { picked = e.execution_id; go("record"); });
      } } }, "Execute these exact terms"));
    }
    if (["CONVERTING", "FUNDS_READY"].indexOf(e.state) >= 0) add(actions, el("button", { type: "button", class: "primary", on: { click: function () { act("/api/advance", { execution_id: e.execution_id }); } } }, "Check progress"));
    if (["EXPIRED", "VOIDED"].indexOf(e.state) >= 0) add(actions, el("a", { class: "button", href: "#stale" }, "See the refusal"));
    var pair = q ? q.buy_currency + "/" + q.sell_currency : null;
    return el("article", { class: "panel request", "aria-label": e.supplier },
      el("h3", null, e.obligation_id + " " + e.supplier + ": " + e.mode + " ", stateChip(e.state),
        q && ["AWAITING_APPROVAL", "APPROVED"].indexOf(e.state) >= 0 ? chip("ok", "LOCKED") : null),
      el("div", { class: "request-grid" },
        // the five numbers an approver needs, beside the control that acts on them
        el("dl", { class: "rows decision" },
          el("dt", null, "Supplier"), el("dd", null, e.supplier),
          el("dt", null, "You pay"), el("dd", { class: "hero-num" }, q ? q.sell_currency + " " + q.sell_amount : e.payout.currency + " " + e.payout.amount + " from the balance"),
          el("dt", null, "Supplier gets"), el("dd", { class: "big" }, e.payout.currency + " " + e.payout.amount),
          pair ? [el("dt", null, "Pair"), el("dd", null, pair)] : null,
          q ? [el("dt", null, "Locked rate"), el("dd", null, q.client_rate + " " + q.sell_currency + " per " + q.buy_currency)] : null,
          q ? [el("dt", null, "Quote valid to"), el("dd", null, hms(q.valid_to_at))] : null,
          el("dt", null, "Approval closes"), el("dd", null, hms(e.expires_at)),
          el("dt", null, "Payout fee"), el("dd", null, e.payout.currency + " " + e.payout.fee + " (" + e.payout.method + ")"),
          el("dt", null, "Calls sent"), el("dd", null, "Conversion " + e.counts.conversion_create + ", payout " + e.counts.transfer_create)),
        el("div", { class: "next" },
          el("p", null, e.state_text),
          q ? el("p", { class: "muted" }, "The approval closes " + e.approval_margin_s + " s before the quote ends, and the deadline is checked again right before the conversion is sent, so a conversion is very unlikely to land on an expired quote.") : null,
          actions)),
      el("p", { class: "inwords", "data-provenance": "code" }, el("strong", null, "In words: "), e.in_words),
      el("details", { class: "allterms" }, el("summary", null, "Quote ID, digest and request IDs"),
        rows([
          q ? ["Quote ID", el("span", { class: "mono", "data-provenance": PROV[state.sources.quotes] }, q.quote_id)] : null,
          ["Digest", digestNode(e.digest)],
          e.conversion_request_id ? ["Conversion request ID", el("span", { class: "mono" }, e.conversion_request_id)] : null,
          ["Payout request ID", el("span", { class: "mono" }, e.transfer_request_id)],
          e.supersedes ? ["Replaces", el("span", { class: "mono" }, e.supersedes.slice(0, 12))] : null,
          e.superseded_by ? ["Replaced by", el("span", { class: "mono" }, e.superseded_by.slice(0, 12))] : null])));
  }
  function approval() {
    var list = state.executions.slice().reverse();
    fill("approval-body",
      el("p", { class: "muted" }, "Server clock (scenario time, ", srcTag(state.sources.quotes), "): ", el("strong", { id: "server-clock", class: "num" }, hms(state.server_now))),
      list.length ? list.map(execPanel) : el("p", { class: "empty" }, "No approval requested yet. On the Plan screen, choose Request approval on a FUND or CONVERT row."),
      controlsPanel([el("button", { type: "button", class: "control", on: { click: function () { act("/api/control/skip-clock", { seconds: 61 }); } } },
        "Skip the fake clock ahead 61 s (time cut)")]));
  }

  // ---- H3 stale ---------------------------------------------------------------------------------------------
  function byId(id) { return state.executions.filter(function (e) { return e.execution_id === id; })[0]; }
  function screenHead(id, title, sub, right) {
    return el("div", { class: "screen-head" }, el("div", null, el("h2", { id: "h-" + id, tabindex: "-1" }, title), sub ? el("p", { class: "sub" }, sub) : null), right || null);
  }
  function hm(iso) { return hms(iso).slice(0, 8); }
  function evOf(e, kind) { return e.events.filter(function (x) { return x.kind === kind; }); }
  function stale() {
    var list = state.executions.filter(function (e) { return e.state === "EXPIRED" || e.state === "VOIDED"; });
    var e = list[list.length - 1];
    if (!e) {
      fill("stale-body", screenHead("stale", "Stale quote refused", "Step 4 of 5. Nothing has been refused yet."),
        el("p", { class: "empty" }, "No refused approval yet. Request approval for a conversion on the Plan screen, let the quote clock run out (or skip the fake clock), then try to approve on the approver page."));
      return;
    }
    var q = e.quote, nx = e.requote ? byId(e.requote.new_execution) : null;
    var calls = e.counts.conversion_create + e.counts.transfer_create;
    var expired = e.state === "EXPIRED";
    var pair = q ? q.sell_currency + " → " + q.buy_currency : e.payout.currency;
    var head = screenHead("stale", expired ? "Stale quote refused" : "Terms changed, request voided",
      e.supplier + " · " + e.invoice_ref + " · " + pair, chip("bad status-chip big-chip", "REFUSED"));
    function lateText() {  // the approval deadline (a margin before the quote ends) is not the quote's own expiry
      var r = evOf(e, "approval_refused").slice(-1)[0];
      if (!r) return "No approval arrived before the approval window closed.";
      return q && new Date(r.at) > new Date(q.valid_to_at) ? "Approval arrived after the quote expired."
        : "Approval arrived after the approval window closed, a few seconds before the quote itself ended.";
    }
    var alertText = calls === 0
      ? [el("span", null, expired ? lateText() : "The terms changed after the request."), el("br"),
         el("span", null, "No conversion or payout was sent to Airwallex ", el("strong", null, "(0 execution API calls)"), ".")]
      : [el("span", null, "Execution calls were already sent for this request: " + calls + ". See the record.")];
    var alert = el("div", { class: "alert bad", role: "status", "data-provenance": "code" }, el("span", { class: "ico-shield", "aria-hidden": "true" }, "◈"), el("p", null, alertText));

    // the two quotes, rate large
    function qcard(title, tag, tagCls, quote, foot, cls) {
      return el("div", { class: "qcard " + (cls || "") },
        el("div", { class: "qhead" }, el("strong", null, title), chip(tagCls, tag)),
        quote ? [el("p", { class: "rate-big num" }, quote.client_rate), el("p", { class: "muted" }, quote.sell_currency + " per " + quote.buy_currency),
          el("p", { class: "strong num" }, quote.sell_currency + " " + quote.sell_amount), el("p", { class: "muted small" }, foot),
          el("p", { class: "mono small muted" }, quote.quote_id)] : null);
    }
    var oldCard = qcard("Old quote", expired ? "Expired" : "Voided", "bad", q, expired ? "Expired " + hm(q ? q.valid_to_at : e.expires_at) + " UTC" : "Terms changed after the request", "old");
    var newCard, costBlock = null, askBlock;
    if (e.requote && nx && nx.quote) {
      newCard = qcard("New quote", "Locked", "info", nx.quote, "Expires " + hm(nx.quote.valid_to_at) + " UTC", "new");
      var dir = e.requote.direction === "less" ? "less" : "more";
      costBlock = el("div", { class: "costbar" },
        el("p", { class: "cost-big num" }, "Costs " + e.requote.currency + " " + e.requote.delta + " " + dir),
        el("p", { class: "muted" }, e.requote.text));
      askBlock = nx.state === "AWAITING_APPROVAL" && nx.approver_path
        ? el("div", { class: "ask" }, el("a", { class: "button primary", href: nx.approver_path, target: "_blank", rel: "noopener noreferrer" }, "Ask approver again"),
            el("span", { class: "muted" }, "A new approval is required before execution."))
        : el("p", { class: "muted" }, "New request: " + nx.state_text);
    } else {
      newCard = el("div", { class: "qcard new empty-card" },
        el("div", { class: "qhead" }, el("strong", null, "New quote")),
        el("p", { class: "muted" }, "No new quote yet. Ask for one to see what the new terms cost."),
        e.mode === "CONVERT" && !e.superseded_by ? el("button", { type: "button", class: "primary", on: { click: function () {
          act("/api/requote", { execution_id: e.execution_id });
        } } }, "Get a new quote") : null);
      askBlock = null;
      if (e.mode === "CONVERT" && !e.superseded_by && state.controls && q) {
        var cur = state.rates.filter(function (r) { return r.pair === q.sell_currency + "/" + q.buy_currency; })[0];
        if (cur) {
          var moved = (parseFloat(cur.sell_per_buy) + 0.01).toFixed(2), fpair = q.buy_currency + q.sell_currency;
          askBlock = controlsPanel([el("button", { type: "button", class: "control", on: { click: function () {
            act("/api/control/fake-rate", { pair: fpair, rate: moved });
          } } }, "Move the fake " + fpair + " rate to " + moved + " (FAKE market move)")]);
        }
      }
    }
    var compare = el("div", { class: "panel compare" },
      el("div", { class: "panel-head" }, el("h3", null, "Quote comparison"), el("span", { class: "chip" }, pair)),
      el("p", { class: "muted" }, "Same recipient. Same " + e.payout.currency + " amount. Amount to receive: ", el("strong", { class: "num" }, e.payout.currency + " " + e.payout.amount)),
      el("div", { class: "qpair" }, oldCard, newCard), costBlock, askBlock);

    // event timeline from the run's real timestamps
    var items = [];
    if (q) items.push({ t: q.valid_from_at, label: "Quote locked" });
    evOf(e, "approval_requested").slice(0, 1).forEach(function (x) { items.push({ t: x.at, label: "Approval requested" }); });
    if (q && expired) items.push({ t: q.valid_to_at, label: "Quote expired", bad: true });
    evOf(e, "approval_refused").slice(-1).forEach(function (x) {
      items.push({ t: x.at, label: x.data.code === "EXPIRED" ? "Approval received (late)" : "Approval refused", bad: true });
    });
    evOf(e, "state").filter(function (x) { return x.data.to === e.state; }).slice(-1).forEach(function (x) {
      items.push({ t: x.at, label: expired ? "Refused" : "Request voided", sub: x.data.why || "Blocked locally", bad: true, solid: true });
    });
    if (nx) items.push({ t: nx.created_at, label: "New quote", sub: nx.quote ? nx.quote.quote_id : null, info: true, solid: true });
    items.sort(function (a, b) { return new Date(a.t) - new Date(b.t); });
    var line = el("ol", { class: "timeline" }, items.map(function (it) {
      return el("li", { class: (it.bad ? "bad " : "") + (it.info ? "info " : "") + (it.solid ? "solid" : "") },
        el("span", { class: "dotm", "aria-hidden": "true" }), el("span", { class: "tl-label" }, it.label,
          it.sub ? el("span", { class: "small muted block" }, it.sub) : null), el("span", { class: "tl-time num" }, hm(it.t)));
    }));
    var side = el("aside", { class: "panel side" },
      el("h3", null, "Event timeline"), el("p", { class: "muted small" }, "All times UTC"), line,
      el("div", { class: "counter-big", "data-provenance": "code" },
        el("p", { class: "strong" }, "Execution API calls"), el("p", { class: "zero num", id: "exec-calls" }, String(calls)),
        el("p", { class: "muted" }, calls === 0 ? "No conversion or payout submitted." : calls + " call(s) were sent for this request.")));
    var refused = evOf(e, "approval_refused").slice(-1)[0];
    var rec = el("div", { class: "recbar" }, el("strong", null, "Decision recorded"),
      refused ? el("span", { class: "mono" }, "Event #" + refused.seq) : null,
      el("span", { class: "muted grow" }, "Quote IDs, timestamps and the refusal code" + (e.refusal ? " (" + e.refusal + ")" : "") + " are saved in the run."),
      el("span", { class: "muted" }, "Step 4 of 5"));
    fill("stale-body", head, alert, el("div", { class: "two-col stale-cols" }, compare, side), rec);
  }

  // ---- H4 record -----------------------------------------------------------------------------------------------
  function executedList() {
    return state.executions.filter(function (e) { return e.counts.conversion_create + e.counts.transfer_create > 0 || e.state === "APPROVED"; });
  }
  function picker(id, list) {
    if (!list.length) return null;
    if (!picked || !byId(picked)) picked = list[list.length - 1].execution_id;
    return el("p", { class: "picker" }, el("label", { for: id }, "Request "), el("select", { id: id, on: { change: function (ev) { picked = ev.target.value; render(); } } },
      list.map(function (e) { return el("option", { value: e.execution_id, selected: e.execution_id === picked }, e.obligation_id + " " + e.supplier + " (" + e.state + ")"); })));
  }
  var KEY_ROWS = ["pair", "sell_amount", "buy_amount", "rate", "quote_id", "payout_amount", "payout_fee", "beneficiary"];
  // amounts read the same in both columns: currency, thousands separators, at least two decimals (a display change only)
  function money(cur, v) {
    var s = String(v);
    if (!cur || !/^\d+(\.\d+)?$/.test(s)) return s;
    var p = s.split("."), d = p[1] || "";
    while (d.length < 2) d += "0";
    return cur + " " + p[0].replace(/\B(?=(\d{3})+(?!\d))/g, ",") + "." + d;
  }
  function rowLabel(f) { return f.replace(/_/g, " ").replace(/^./, function (c) { return c.toUpperCase(); }); }
  function exportRecord() {
    showError(null);
    currentRecord().then(function (rec) {
      var blob = new Blob([JSON.stringify(rec, null, 2)], { type: "application/json" });
      var a = el("a", { href: URL.createObjectURL(blob), download: "bundle.json" });
      document.body.appendChild(a);
      a.click();
      a.remove();
      say("Exported bundle.json");
    }).catch(showError);
  }
  var recGen = 0, verifyGen = 0;   // a later load or verify supersedes an earlier one that is still in flight
  function record() {
    var gen = ++recGen;
    var list = executedList();
    if (!list.length) {
      fill("record-body", screenHead("record", "Record and verify", "Step 5 of 5. Nothing has been executed yet."),
        el("p", { class: "empty" }, "Nothing executed yet. Approve a request on the approver page, then choose Execute on the Locked quote screen."));
      fill("record-detail-body");
      return;
    }
    var pick = picker("pick-record", list);
    var e = byId(picked);
    fill("record-body", screenHead("record", "Record and verify", "Step 5 of 5", pick), el("p", { class: "muted" }, "Loading the record..."));
    api("/api/record", { execution_id: e.execution_id }).then(function (rec) {
      if (gen !== recGen) return null;
      records[e.execution_id] = rec;
      // the banner is bound to the offline check: the same verify the exported copy gets
      return api("/api/verify", { bundle: rec }).then(function (out) { return gen !== recGen ? null : [rec, out]; });
    }).then(function (rv) {
      if (!rv || gen !== recGen) return;
      var rec = rv[0], out = rv[1];
      var failed = e.state === "PAYOUT_FAILED";
      var comparable = rec.rows.filter(function (r) { return r.field.slice(-7) !== "_status"; });
      var passed = comparable.filter(function (r) { return r.matches === true; }).length;
      var summary = !rec.complete ? "Not complete yet: some executed values are still missing."
        : rec.all_match ? (failed ? "Approved terms match. The payout failed and has not been retried." : "Every term matches what the person approved.")
        : "Some terms differ from what the person approved.";
      var tick = function (r) {
        return r.executed === "-" ? el("span", { class: "muted" }, "not yet")
          : r.matches ? el("span", { class: "tick ok" }, "✓", el("span", { class: "sr-only" }, " matches"))
          : el("span", { class: "tick bad" }, "✗", el("span", { class: "sr-only" }, " differs"));
      };
      var keyRows = rec.rows.filter(function (r) { return KEY_ROWS.indexOf(r.field) >= 0; });
      var kt = el("tbody");
      var curOf = { sell_amount: e.quote && e.quote.sell_currency, buy_amount: e.quote && e.quote.buy_currency,
        payout_amount: e.payout.currency, payout_fee: e.payout.currency };
      var shown = function (r, v, executedColumn) {
        if (v === "-") return v;
        if (r.field === "beneficiary") {
          var fromRequest = executedColumn && rec.provider && rec.provider.transfer && rec.provider.transfer.beneficiary_from === "request";
          return [el("span", { class: "benname" }, e.supplier), el("span", { class: "subid mono" }, v),
            fromRequest ? el("span", { class: "subid" }, "from this app's own request: the provider's reply named no beneficiary") : null];
        }
        return curOf[r.field] ? money(curOf[r.field], v) : v;
      };
      keyRows.forEach(function (r) {
        add(kt, el("tr", null, el("th", { scope: "row" }, rowLabel(r.field)),
          el("td", { class: "num mono", "data-provenance": "code" }, shown(r, r.approved, false)),
          el("td", { class: "num mono", "data-provenance": PROV[state.sources.balances] }, shown(r, r.executed, true)),
          el("td", { class: "mid" }, tick(r))));
      });
      var q = e.quote, evOne = function (k) { return evOf(e, k)[0]; };
      var approvedAt = evOne("approved"), executedAt = evOne("conversion_created") || evOne("transfer_created");
      var times = [approvedAt ? "Approved " + hm(approvedAt.at) + " UTC" : null, executedAt ? "Executed " + hm(executedAt.at) + " UTC" : null,
        q ? "Quote ends " + hm(q.valid_to_at) + " UTC" : null].filter(Boolean);
      var approved = el("div", { class: "panel list" },
        el("div", { class: "panel-head" }, el("div", null, el("h3", null, "Approved vs executed"),
          el("p", { class: "muted" }, e.supplier + " · Invoice " + e.invoice_ref)),
          el("strong", { class: "checks-count " + (passed === comparable.length && rec.complete ? "ok" : "warn"), id: "checks-count" },
            passed + " of " + comparable.length + " checks passed")),
        el("div", { class: "table-wrap" }, el("table", { class: "key-table" },
          el("thead", null, el("tr", null, el("th", null, "Detail"), el("th", null, "Approved"), el("th", null, "Executed"), el("th", { class: "mid" }, "Match"))), kt)),
        el("p", { class: "timesbar" }, "◷ " + times.join(" · ")));
      var prov = rec.provider;
      var refsTitle = state.world === "FAKE" ? "FAKE world references (local, not Airwallex)" : "Airwallex sandbox references";
      var refs = el("div", { class: "panel" }, el("h3", null, refsTitle),
        el("dl", { class: "rows refs" },
          prov.conversion ? [el("dt", null, "Conversion ID"), el("dd", { class: "mono", "data-provenance": PROV[state.sources.balances] }, prov.conversion.conversion_id)] : null,
          prov.transfer ? [el("dt", null, "Transfer ID"), el("dd", { class: "mono", "data-provenance": PROV[state.sources.balances] }, prov.transfer.transfer_id)] : null,
          !prov.conversion && !prov.transfer ? [el("dt", null, "Provider objects"), el("dd", null, "none yet")] : null));
      var tcreated = evOf(e, "transfer_created")[0];
      var lastState = evOf(e, "state").filter(function (x) { return ["PAYOUT_REPORTED", "PAID", "PAYOUT_FAILED"].indexOf(x.data.to) >= 0; }).slice(-1)[0];
      var tstat = [];
      if (prov.transfer) {
        var st0 = tcreated ? tcreated.data.status : null, st1 = prov.transfer.status;
        if (st0 && st0 !== st1) tstat.push(el("div", { class: "stat" }, chip("info", st0), el("span", { class: "small muted" }, hm(tcreated.at) + " UTC")), el("span", { class: "stat-line", "aria-hidden": "true" }));
        tstat.push(el("div", { class: "stat" }, chip(st1 === "PAID" ? "ok" : (st1 === "FAILED" ? "bad" : "info"), st1),
          el("span", { class: "small muted" }, (lastState ? hm(lastState.at) : hm(rec.generated_at)) + " UTC")));
      }
      var tact = el("div", { class: "row-actions" });
      if (e.transfer && ["PAYOUT_REPORTED", "PAID"].indexOf(e.state) >= 0) {
        add(tact, el("button", { type: "button", on: { click: function () { act("/api/advance", { execution_id: e.execution_id }); } } }, e.state === "PAID" ? "Check payout status again" : "Check payout status"));
        if (e.state === "PAYOUT_REPORTED") add(tact, el("button", { type: "button", class: "control", on: { click: function () {
          act("/api/transition", { execution_id: e.execution_id, status: "PAID" });
        } } }, "Simulate transfer PAID (transfer simulation endpoint, " + state.sources.balances + ")"));
        else add(tact, el("button", { type: "button", class: "control", on: { click: function () {
          act("/api/transition", { execution_id: e.execution_id, status: "FAILED" });
        } } }, "Simulate a late FAILED after PAID (transfer simulation endpoint, " + state.sources.balances + ")"));
      }
      var transfer = el("div", { class: "panel" }, el("h3", null, "Transfer status"),
        tstat.length ? el("div", { class: "statline" }, tstat) : el("p", { class: "muted" }, "No supplier payment yet."),
        el("p", { class: "notice " + (failed ? "bad" : (e.state === "PAID" ? "ok" : "")), role: "status" }, e.state_text), tact);
      var verdictPass = out.verdict === "PASS";
      var banner;
      if (!verdictPass) {
        banner = el("div", { class: "vbanner bad", id: "verify-banner", "data-verdict": out.verdict, role: "status" },
          el("span", { class: "vmark", "aria-hidden": "true" }, "✗"),
          el("div", null, el("p", { class: "vsmall" }, "Verify record"), el("p", { class: "vbig" }, "FAIL: the record does not verify"), el("p", { class: "muted" }, out.problems.join("; "))));
      } else if (!rec.complete) {
        banner = el("div", { class: "vbanner neutral", id: "verify-banner", "data-verdict": out.verdict, role: "status" },
          el("span", { class: "vmark", "aria-hidden": "true" }, "…"),
          el("div", null, el("p", { class: "vsmall" }, "Verify record"), el("p", { class: "vbig" }, "Not complete yet"), el("p", { class: "muted" }, "Some executed values are still missing.")));
      } else if (!rec.all_match || failed) {
        banner = el("div", { class: "vbanner warn", id: "verify-banner", "data-verdict": out.verdict, role: "status" },
          el("span", { class: "vmark", "aria-hidden": "true" }, "!"),
          el("div", null, el("p", { class: "vsmall" }, "Verify record"), el("p", { class: "vbig" }, failed ? "Terms match; the payout failed" : "Some terms differ"), el("p", { class: "muted" }, summary)));
      } else {
        banner = el("div", { class: "vbanner pass", id: "verify-banner", "data-verdict": out.verdict, role: "status" },
          el("span", { class: "vmark", "aria-hidden": "true" }, "✓"),
          el("div", { class: "grow" }, el("p", { class: "vsmall" }, "Verify record"), el("p", { class: "vbig" }, "PASS: every executed term matches the approved terms"),
            el("p", { class: "muted" }, "Digest and rows verify. An exported copy can be verified offline.")),
          el("span", { class: "chip" }, "SHA-256 " + rec.record_digest.slice(0, 12) + "…"));
      }
      var right = el("div", { class: "head-right" }, pick, el("button", { type: "button", class: "primary", id: "btn-export-top", on: { click: exportRecord } }, "Export record"));
      var titleChip = verdictPass && rec.complete && rec.all_match && !failed ? chip("ok status-chip big-chip", "✓ VERIFIED") : null;
      var head = el("div", { class: "screen-head" }, el("div", null,
        el("h2", { id: "h-record", tabindex: "-1" }, "Record and verify ", titleChip), el("p", { class: "sub" }, summary)), right);
      fill("record-body", head, approved, el("div", { class: "two-col even" }, refs, transfer), banner,
        el("p", { class: "recfoot muted small" }, "Record " + rec.record_digest.slice(0, 8) + " · generated " + when(rec.generated_at) + " · ",
          el("a", { href: "#verify" }, "Lost a response? Look up by request ID, or verify an exported copy")));
      // details: every row, the reserve, the old approval link
      var tb = el("tbody");
      rec.rows.forEach(function (r) {
        var status = r.field.slice(-7) === "_status";
        var check = r.executed === "-" ? el("span", { class: "muted" }, "not yet")
          : status ? el("span", { class: "muted" }, "as reported")
          : r.matches ? el("span", { class: "ok" }, "✓ matches") : el("span", { class: "bad" }, "✗ differs");
        add(tb, el("tr", null, el("th", { scope: "row" }, r.field.replace(/_/g, " ")),
          el("td", { class: "num mono", "data-provenance": "code" }, r.approved),
          el("td", { class: "num mono", "data-provenance": PROV[state.sources.balances] }, r.executed + (status && r.executed !== "-" ? " (reported by the provider, " + state.sources.balances + ")" : "")),
          el("td", null, check)));
      });
      fill("record-detail-body",
        el("div", { class: "table-wrap" }, el("table", null,
          el("thead", null, el("tr", null, el("th", null, "Field"), el("th", { class: "num" }, "Approved"), el("th", { class: "num" }, "Executed"), el("th", null, "Check by code"))), tb)),
        el("p", null, summary, " Record digest ", el("span", { class: "mono" }, rec.record_digest.slice(0, 16) + "...")),
        e.approver_path ? el("p", null, el("a", { class: "button", href: e.approver_path, target: "_blank", rel: "noopener noreferrer" }, "Open the old approval link")) : null,
        el("div", { class: "panel" }, el("h3", null, "Remaining reserve after execution"),
          source("Source: ", srcTag("CODE"), " from ", srcTag(state.sources.balances), " balances"), reserveTable(state.reserve)));
    }).catch(function (err) { if (gen === recGen) showError(err); });
  }

  // ---- H5 lookup ------------------------------------------------------------------------------------------------------
  function lookup() {
    var list = executedList();
    var sw = controlsPanel([el("button", { type: "button", class: "control", on: { click: function () {
      act("/api/control/withhold", { op: "create_transfer" }, function () { say("Test switch on: the next supplier payment response will be withheld."); });
    } } }, "Withhold the next supplier payment response (test switch)")]);
    if (!list.length) {
      fill("lookup-body", el("p", { class: "empty" }, "Nothing executed yet. To see a lost response: switch on the test switch below, then request, approve and execute a payment (for example O1 from the balance)."), sw);
      return;
    }
    var pick = picker("pick-lookup", list);
    var e = byId(picked);
    var kinds = e.events.map(function (x) { return x.kind; });
    var has = function (k) { return kinds.indexOf(k) >= 0; };
    var lookups = kinds.filter(function (k) { return k.indexOf("lookup_") === 0; }).length;
    var steps = [
      [has("transfer_dispatching") || has("conversion_dispatching"), "Created: intent written before the call, with a pre-minted request ID"],
      [has("outcome_unknown"), has("outcome_unknown") ? "Not sure yet: response withheld by our test switch (" + state.sources.balances + " world only)" : "Not sure yet: did not happen for this request"],
      [has("lookup_found"), "Found by request ID"]];
    var actions = el("div", { class: "row-actions" });
    if (e.state === "UNKNOWN" || e.state === "INDETERMINATE") {
      add(actions, el("button", { type: "button", class: "primary", on: { click: function () { act("/api/reconcile", { execution_id: e.execution_id }); } } }, "Look up by request ID"));
    }
    fill("lookup-body", pick,
      el("div", { class: "panel" }, el("h3", null, e.obligation_id + " " + e.supplier),
        el("ol", { class: "ladder" }, steps.map(function (s) { return el("li", { class: s[0] ? "done" : null }, s[1]); })),
        el("p", { class: "statement" }, e.state_text),
        el("p", { class: "counter", "data-provenance": "code" }, "Payout create calls for this request ID: " + e.counts.transfer_create + ". Lookups: " + lookups + ". Never resent."),
        actions),
      sw);
  }

  // ---- Verify ------------------------------------------------------------------------------------------------------------
  function currentRecord() {
    var e = picked && byId(picked);
    if (!e) return Promise.reject(new Error("Execute a request first; then its record can be exported."));
    return api("/api/record", { execution_id: e.execution_id });
  }
  document.getElementById("btn-export").addEventListener("click", exportRecord);
  document.getElementById("btn-load").addEventListener("click", function () {
    showError(null);
    currentRecord().then(function (rec) { document.getElementById("bundle").value = JSON.stringify(rec, null, 2); say("Record loaded into the box"); }).catch(showError);
  });
  document.getElementById("btn-tamper").addEventListener("click", function () {
    showError(null);
    var box = document.getElementById("bundle");
    var p = box.value.trim() ? Promise.resolve(JSON.parse(box.value)) : currentRecord();
    p.then(function (rec) {
      var row = rec.rows.filter(function (r) { return r.field === "rate"; })[0] || rec.rows.filter(function (r) { return r.field === "payout_amount"; })[0];
      if (!row || row.executed === "-") throw new Error("This record has no executed rate or amount to edit yet.");
      var before = row.executed;
      row.executed = (parseFloat(before) + 0.01).toFixed(Math.max(2, (before.split(".")[1] || "").length));
      box.value = JSON.stringify(rec, null, 2);
      fill("verify-result", el("p", { class: "muted" }, "Edited copy: " + row.field + " executed changed from " + before + " to " + row.executed + ". Now choose Verify."));
    }).catch(showError);
  });
  document.getElementById("btn-verify").addEventListener("click", function () {
    showError(null);
    var bundle;
    try { bundle = JSON.parse(document.getElementById("bundle").value); } catch (err) { showError(new Error("The box does not hold valid JSON.")); return; }
    var mine = ++verifyGen;
    api("/api/verify", { bundle: bundle }).then(function (out) {
      if (mine !== verifyGen) return;
      var pass = out.verdict === "PASS";
      fill("verify-result",
        el("p", { class: "verdict " + (pass ? "pass" : "fail"), "data-provenance": "code" },
          pass ? "PASS: this copy matches its stored digest and recorded rows." : "FAIL\n" + out.problems.join("\n")),
        el("p", { class: "muted" }, "Verify checks the copy against its stored digest and recorded provider fields. It does not authenticate the record or its source."));
    }).catch(showError);
  });

  // ---- calls ------------------------------------------------------------------------------------------------------------------
  function calls() {
    var tb = el("tbody");
    state.calls.forEach(function (c) {
      add(tb, el("tr", null, el("td", { class: "num" }, c.seq), el("td", { class: "mono" }, c.op),
        el("td", { class: "mono" }, c.request_id || "-"), el("td", null, c.outcome)));
    });
    var log = state.controls_log.map(function (c) { return el("li", null, hms(c.at) + " " + c.text); });
    fill("calls-body", el("div", { class: "table-wrap", "data-provenance": "code" }, el("table", null,
      el("thead", null, el("tr", null, el("th", { class: "num" }, "#"), el("th", null, "Call"), el("th", null, "Request ID"), el("th", null, "Outcome"))), tb)),
      log.length ? [el("h3", null, "Test controls used"), el("ul", null, log)] : null);
  }

  // ---- guide: where you are in the story (text only; the decisions are made elsewhere) ------------------------
  function guide() {
    var s = state, ex = s.executions, last = ex[ex.length - 1], g, to = null;
    var fake = s.controls;
    var screen = location.hash.slice(1);
    if ((screen === "record" || screen === "verify") && picked && byId(picked)) last = byId(picked);
    if (last && last.state === "PAYOUT_FAILED") {
      g = "Payment failed. It was not retried. Review the provider status and the record; a person decides what happens next."; to = "record";
    } else if (s.review.pending.length) {
      g = "Step 1 of 5, Invoices in. " + waitingText().replace("The plan waits: ", "") + " Choose the time zone, write a note, press Confirm deadline."; to = "invoices";
    } else if (!ex.length && s.plans.length < 2) {
      g = "Step 2 of 5, Plan. Read plan v1, then open Demo controls and press Receipt R1 less certain: only the decisions that depend on that receipt are recomputed."; to = "plan";
    } else if (!ex.length && s.plans.length < 3) {
      g = "Step 2 of 5, Plan. Now open Demo controls, press Simulate deposit USD 1,000 and watch Initech move from escalate to convert."; to = "plan";
    } else if (!ex.length) {
      g = "Step 2 of 5, Plan. On the Globex row press Get a locked quote and request approval."; to = "plan";
    } else if (last.state === "AWAITING_APPROVAL") {
      g = "Step 3 of 5, Locked quote. Open the approver page. " + (fake ? "Let the quote run out (or press Skip the fake clock) and try to approve anyway." : "Approve while the quote clock is running, or wait it out to see the refusal."); to = "approval";
    } else if (["EXPIRED", "VOIDED"].indexOf(last.state) >= 0 && !last.superseded_by) {
      g = "Step 4 of 5, Approval. No conversion or payout was sent. Press Get a new quote to see what the new terms cost."; to = "stale";
    } else if (["EXPIRED", "VOIDED"].indexOf(last.state) >= 0) {
      g = "Step 4 of 5, Approval. Ask the approver again on the approver page."; to = "stale";
    } else if (last.state === "APPROVED") {
      g = "Step 4 of 5, Approval. Approved. Press Execute these exact terms."; to = "approval";
    } else if (last.state === "UNKNOWN" || last.state === "INDETERMINATE") {
      g = "Step 5 of 5, Record. The outcome is not known. Look it up by request ID; never resend."; to = "verify";
    } else if (last.state === "PAYOUT_REPORTED") {
      g = "Step 5 of 5, Record. The payout is reported but not final. Check its status" + (fake ? " or simulate PAID." : "."); to = "record";
    } else if (last.state === "PAID") {
      g = "Step 5 of 5, Record. PAID is the provider's report, not a final settlement: a paid transfer can still fail, so check it again. Then compare approved with executed and export the record."; to = "record";
    } else {
      g = "Step 5 of 5, Record. Compare approved with executed, then export the record and Verify it (try editing one value first)."; to = "record";
    }
    var n = document.getElementById("guide");
    while (n.firstChild) n.removeChild(n.firstChild);
    add(n, el("strong", null, "Guide: "));
    add(n, g + " ");
    if (to && location.hash.slice(1) !== to) add(n, el("a", { href: "#" + to }, "Go there"));
  }

  // ---- render and clock ---------------------------------------------------------------------------------------------------------
  function render() {
    chips(); invoices(); plan(); approval(); stale(); record(); lookup(); calls(); guide(); route();
  }
  function tick() {
    var n = document.getElementById("server-clock");
    if (!n || !state) return;
    var now = new Date(new Date(state.server_now).getTime() + (performance.now() - fetchedAt));
    n.textContent = hms(now.toISOString());
  }
  document.getElementById("btn-lower").addEventListener("click", function () { act("/api/lower-confidence", { receipt_id: "R1" }); });
  document.getElementById("btn-deposit").addEventListener("click", function () { act("/api/deposit", { currency: "USD", amount: "1000" }); });
  if (window.matchMedia) {
    var mq = window.matchMedia("(max-width: 1099px)");
    var again = function () { if (state) render(); };
    if (mq.addEventListener) mq.addEventListener("change", again); else if (mq.addListener) mq.addListener(again);
  }
  route();
  refresh(true);
  setInterval(function () { if (!busy) refresh(false); }, 2000);
  setInterval(tick, 500);
})();

"use strict";
/* Approver page. Knows only its own link token. The ring and numeral are computed from the quote's end time
   and the server clock (re-read every 3 s), never from a timer started at page load. */
(function () {
  var PROV = { FAKE: "fake", SANDBOX: "sandbox" };
  var token = location.pathname.split("/").pop();
  var base = "/api/approver/" + encodeURIComponent(token);
  var view = null, fetchedAt = 0, lastSay = -1, sending = false;
  var C = 2 * Math.PI * 88;
  var reduced = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var params = new URLSearchParams(location.search);
  if (params.get("stage") === "video") document.body.classList.add("stage");

  function $(id) { return document.getElementById(id); }
  function add(n, c) {
    if (c === null || c === undefined) return;
    n.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
  }
  function el(tag, cls) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    for (var i = 2; i < arguments.length; i++) add(n, arguments[i]);
    return n;
  }
  function hms(iso) {
    var d = new Date(iso);
    function p(x) { return (x < 10 ? "0" : "") + x; }
    return p(d.getUTCHours()) + ":" + p(d.getUTCMinutes()) + ":" + p(d.getUTCSeconds()) + " UTC";
  }
  function serverNow() { return new Date(view.server_now).getTime() + (performance.now() - fetchedAt); }

  function post(path, body) {
    return fetch(base + path, { method: "POST", credentials: "omit", headers: { "Content-Type": "application/json" },
                                body: JSON.stringify(body) })
      .then(function (r) { return r.json().then(function (j) { if (!r.ok) throw new Error(j.message || j.error); return j; }); });
  }
  function load() {
    return fetch(base, { credentials: "omit" }).then(function (r) {
      return r.json().then(function (j) { if (!r.ok) throw new Error(j.message || j.error); return j; });
    }).then(function (v) { view = v; fetchedAt = performance.now(); render(); }).catch(function (e) {
      $("message").textContent = e.message;
      $("message").className = "notice bad";
      $("confirm").hidden = true;
    });
  }

  function row(dl, label, value, cls) {
    add(dl, el("dt", null, label));
    add(dl, el("dd", cls || null, value));
  }
  function clear(dl) { while (dl.firstChild) dl.removeChild(dl.firstChild); }
  // the numbers an approver needs, next to the approve control: pay, get, locked rate, fee, beneficiary
  function decision() {
    var v = view, trade = $("trade"), rate = $("raterow"), dl = $("decision"), why = $("why");
    clear(trade); clear(rate); clear(dl); clear(why);
    var convert = !!v.rate;
    add(trade, el("dt", null, convert ? "You pay" : "You pay from the balance"));
    add(trade, el("dd", "hero-num", v.you_pay.currency + " " + v.you_pay.amount));
    if (convert) add(trade, el("dt", "arrow", "↓"));
    add(trade, el("dt", null, "Supplier gets"));
    add(trade, el("dd", "hero-num second", v.supplier_gets.currency + " " + v.supplier_gets.amount));
    if (convert) {
      var p = v.pair.split("/");
      add(rate, el("span", "rate", "1 " + p[0] + " = " + v.rate + " " + p[1]));
      var locked = ["live", "too_close", "used"].indexOf(v.state) >= 0;
      add(rate, el("span", "chip " + (locked ? "ok" : "bad") + " lockchip", locked ? "LOCKED" : "EXPIRED"));
      add(rate, el("span", "pairnote", "Locked rate, " + v.pair));
    } else {
      add(rate, el("span", "rate", "No conversion: paid from the " + v.supplier_gets.currency + " balance"));
    }
    row(dl, "Payout fee", v.supplier_gets.currency + " " + v.fee + " (" + v.payout_method + ")");
    row(dl, "Beneficiary", v.supplier + " · " + v.invoice_ref);
    var because = convert
      ? "Convert " + v.pair.split("/")[1] + " into " + v.pair.split("/")[0] + " to pay " + v.supplier + " " + v.supplier_gets.currency + " " + v.supplier_gets.amount + " for invoice " + v.invoice_ref + "."
      : "Pay " + v.supplier + " " + v.supplier_gets.currency + " " + v.supplier_gets.amount + " for invoice " + v.invoice_ref + " from the " + v.supplier_gets.currency + " balance.";
    add(why, el("strong", null, "Why"));
    add(why, el("span", null, because));
  }
  function terms() {
    var dl = $("terms"), v = view;
    clear(dl);
    row(dl, "You pay", v.you_pay.currency + " " + v.you_pay.amount + " (" + v.you_pay.note + ")");
    if (v.from_balance && v.from_balance !== "0.00") row(dl, "Also from balance", v.supplier_gets.currency + " " + v.from_balance);
    if (v.rate) row(dl, "Quote expires", hms(v.valid_to_at));
    if (v.quote_id) row(dl, "Quote ID", v.quote_id, "mono");
    row(dl, "Approval closes", hms(v.expires_at));
    row(dl, "Digest", v.digest.slice(0, 8) + " " + v.digest.slice(8, 24) + "...", "mono");
  }
  var STATE_CHIP = {
    live: ["Open for approval", "ok"], too_close: ["Too close to expiry", "warn"], expired: ["Expired", "bad"],
    used: ["Approval recorded", "ok"], denied: ["Denied", "bad"], terms_changed: ["Terms changed", "bad"] };

  function render() {
    var v = view;
    var w = v.chips.world;
    $("chip-world").textContent = "WORLD: " + w;
    $("chip-world").setAttribute("data-provenance", PROV[v.world] || "fake");
    $("chip-money").textContent = v.chips.money;
    $("chip-run").textContent = "RUN: " + v.chips.run;
    $("src-world").textContent = v.world;
    $("src-world").setAttribute("data-provenance", PROV[v.world] || "fake");
    $("title").textContent = v.rate ? "Approve conversion" : "Approve supplier payment";
    $("subtitle").textContent = v.rate ? "Review the locked quote before approving." : "Review the exact terms before approving.";
    $("inwords").textContent = v.in_words || "";
    var sc = STATE_CHIP[v.state] || ["Loading", ""];
    $("state-chip").textContent = sc[0];
    $("state-chip").className = "chip status " + sc[1];
    decision();
    terms();
    $("prefix").textContent = v.digest.slice(0, v.digest_prefix_len);
    $("prefix-len").textContent = String(v.digest_prefix_len);
    var msg = $("message");
    msg.textContent = v.message;
    var cls = { live: "notice ok", too_close: "notice warn", expired: "notice bad", used: "notice", denied: "notice bad", terms_changed: "notice bad" };
    msg.className = cls[v.state] || "notice";
    msg.hidden = v.state === "live";  // the card says the same thing; keep the decision above the fold on a phone
    var open = ["live", "too_close", "expired"].indexOf(v.state) >= 0;
    $("confirm").hidden = !open;
    $("approve").textContent = v.state === "live" ? "Approve" : "Send approval anyway";
    $("approve").className = v.state === "live" ? "primary" : "";
    $("approve-hint").hidden = v.state === "live";
    tick();
  }

  function tick() {
    if (!view) return;
    // the approval closes before the quote ends (the dispatch margin), so the clock follows the approval deadline
    var from = new Date(view.valid_from_at).getTime(), to = new Date(view.expires_at).getTime();
    var leftMs = Math.max(0, to - serverNow());
    var total = Math.max(1, to - from);
    var secs = Math.ceil(leftMs / 1000);
    var ringSecs = reduced ? Math.ceil(secs / 10) * 10 : leftMs / 1000;
    var final = ["used", "denied", "terms_changed"].indexOf(view.state) >= 0;
    var frac = final ? 0 : Math.max(0, Math.min(1, (ringSecs * 1000) / total));
    var ring = $("ring");
    ring.setAttribute("stroke-dasharray", String(C));
    ring.setAttribute("stroke-dashoffset", String(C * (1 - frac)));
    $("left").textContent = final ? "-" : (leftMs <= 0 ? "0:00" : Math.floor(secs / 60) + ":" + ("0" + (secs % 60)).slice(-2));
    $("until").textContent = final ? "Request closed" : leftMs <= 0 ? "Approval window closed " + hms(view.expires_at) : "Approval closes " + hms(view.expires_at);
    $("under").textContent = !final && leftMs > 0 && secs < 15 ? "Under 15 seconds" : "";
    $("clock").className = "clock" + (!final && secs < 15 ? " urgent" : "");
    if (!final && secs % 15 === 0 && secs !== lastSay) {
      lastSay = secs;
      $("clock-say").textContent = leftMs <= 0 ? "The approval window has closed." : secs + " seconds left to approve.";
    }
    var expiresAt = new Date(view.expires_at).getTime();
    if (view.state === "live" && serverNow() >= expiresAt) load();
  }

  $("confirm").addEventListener("submit", function (ev) {
    ev.preventDefault();
    if (sending || !view) return;
    sending = true;
    $("result").textContent = "";
    post("/approve", { digest: view.digest, typed: view.digest.slice(0, view.digest_prefix_len) }).then(function (out) {
      $("result").textContent = out.ok ? "Approved. The console can now execute these exact terms." : out.message + " (" + out.code + ")";
      $("result").className = out.ok ? "notice ok" : "notice bad";
      view = out.view;
      fetchedAt = performance.now();
      return load();
    }).catch(function (e) { $("result").textContent = e.message; $("result").className = "notice bad"; }).then(function () { sending = false; });
  });
  $("deny").addEventListener("click", function () {
    if (sending || !view) return;
    sending = true;
    post("/deny", {}).then(function (out) { $("result").textContent = out.message; $("result").className = "notice"; return load(); })
      .catch(function (e) { $("result").textContent = e.message; $("result").className = "notice bad"; }).then(function () { sending = false; });
  });

  load();
  setInterval(function () { if (!sending) load(); }, 3000);
  setInterval(tick, 250);
})();

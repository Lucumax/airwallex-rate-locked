"""Multi-obligation plan card (prototype). One static HTML file, no scripts, no network, no fonts to load.

Renders plan versions side by side with what changed: obligations the replanner did not touch are greyed and
labelled "Not recomputed". Tokens follow the design section of the spec (light and dark).
Every figure comes from the plan objects handed in. The card carries the PROTOTYPE watermark and the
independent-project notice.
"""
from __future__ import annotations

import html
from decimal import Decimal

from . import NOTICE
from .models import Action

E = html.escape

TOKENS_CSS = """
:root { --bg:#F7F8FA; --surface:#FFFFFF; --ink:#1A1D21; --muted:#5C636D; --line:#E8EAED; --edge:#868E98;
  --accent:#612FFF; --accent-strong:#4B1FD6; --ok:#0B7A47; --warn:#B54708; --bad:#C4261C; --clock:#E8590C;
  --focus:#612FFF; --radius:8px; --radius-pill:9999px; --target:44px;
  --font-ui:"Inter", system-ui, -apple-system, "Segoe UI", sans-serif;
  --font-mono:"IBM Plex Mono", ui-monospace, "Cascadia Mono", Consolas, monospace; }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) {
  --bg:#0B0B0D; --surface:#151619; --ink:#ECECE8; --muted:#9C9C96; --line:#2A2C31; --edge:#6B6E75;
  --accent:#A28BFF; --accent-strong:#A28BFF; --ok:#4FD196; --warn:#FFB060; --bad:#FF8A80; --clock:#FF6A47;
  --focus:#A28BFF; } }
:root[data-theme="dark"] {
  --bg:#0B0B0D; --surface:#151619; --ink:#ECECE8; --muted:#9C9C96; --line:#2A2C31; --edge:#6B6E75;
  --accent:#A28BFF; --accent-strong:#A28BFF; --ok:#4FD196; --warn:#FFB060; --bad:#FF8A80; --clock:#FF6A47;
  --focus:#A28BFF; }
"""

BASE_CSS = """
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--ink); font:16px/1.5 var(--font-ui); }
a:focus, button:focus, .skip:focus { outline:3px solid var(--focus); outline-offset:2px; }
.skip { position:absolute; left:-999px; top:8px; background:var(--surface); color:var(--ink); padding:8px 12px;
  border:1px solid var(--edge); border-radius:var(--radius); }
.skip:focus { left:16px; z-index:10; }
.topbar { display:flex; flex-wrap:wrap; gap:8px 12px; align-items:center; padding:12px 16px;
  background:var(--surface); border-bottom:1px solid var(--line); }
.brand { font-weight:600; margin-right:auto; }
.chip { display:inline-flex; gap:6px; align-items:center; border:1px solid var(--edge); border-radius:var(--radius-pill);
  padding:2px 10px; font-size:13px; font-weight:500; background:var(--surface); white-space:nowrap; }
.chip.fund { color:var(--ok); border-color:var(--ok); }
.chip.convert { color:var(--accent-strong); border-color:var(--accent-strong); }
.chip.defer { color:var(--warn); border-color:var(--warn); }
.chip.escalate { color:var(--bad); border-color:var(--bad); }
.layout { display:grid; grid-template-columns:1fr; gap:24px; max-width:1280px; margin:0 auto; padding:16px; }
.rail { display:none; }
.rail ol { list-style:none; margin:0; padding:0; }
.rail li { padding:8px 12px; color:var(--muted); border-left:3px solid var(--line); }
.rail li[aria-current="step"] { color:var(--accent-strong); border-left-color:var(--accent); font-weight:600; }
@media (min-width: 1000px) { .layout { grid-template-columns:200px 1fr; } .rail { display:block; } }
h1 { font-size:26px; line-height:1.25; margin:8px 0 4px; font-weight:600; }
h2 { font-size:20px; margin:0 0 8px; font-weight:600; }
h3 { font-size:17px; margin:0; font-weight:600; }
.lede { color:var(--muted); margin:0 0 16px; max-width:70ch; }
.panel { background:var(--surface); border:1px solid var(--line); border-radius:var(--radius); padding:16px 20px; margin-bottom:20px; }
.balances { display:flex; flex-wrap:wrap; gap:8px 24px; margin:8px 0 0; padding:0; list-style:none; }
.balances li { font-variant-numeric:tabular-nums; }
.balances .ccy { color:var(--muted); margin-right:6px; }
.triggers { list-style:none; margin:0 0 12px; padding:0; }
.triggers li { padding:2px 0; }
.mark { display:inline-block; width:1.4em; font-family:var(--font-mono); font-weight:700; }
.cards { display:grid; grid-template-columns:1fr; gap:12px; }
@media (min-width: 760px) { .cards { grid-template-columns:repeat(3, 1fr); } }
.card { background:var(--surface); border:1px solid var(--line); border-radius:var(--radius); padding:16px; }
.card.reused { border-style:dashed; border-color:var(--edge); background:var(--bg); }
.card.changed { border-color:var(--edge); box-shadow:0 1px 4px rgba(0,0,0,.12); }
.card header { display:flex; flex-direction:column; gap:2px; margin-bottom:8px; }
.ref { color:var(--muted); font-size:14px; }
.amount { font-size:24px; font-weight:600; margin:0 0 8px; font-variant-numeric:tabular-nums; }
.amount .ccy { color:var(--muted); font-size:16px; margin-right:6px; font-weight:500; }
.decision { display:flex; flex-wrap:wrap; gap:8px; align-items:center; margin:0 0 8px; }
.recalc { color:var(--muted); font-size:13px; }
.why { margin:0 0 8px; }
dl.rows { margin:0; display:grid; grid-template-columns:auto 1fr; gap:4px 12px; font-size:14px; }
dl.rows dt { color:var(--muted); }
dl.rows dd { margin:0; text-align:right; font-variant-numeric:tabular-nums; }
.was { margin:8px 0 0; font-size:14px; color:var(--muted); }
.was strong { color:var(--ink); }
footer { max-width:1280px; margin:0 auto; padding:8px 16px 40px; color:var(--muted); font-size:14px; }
footer p { margin:4px 0; }
.watermark { position:fixed; inset:0; display:grid; place-items:center; pointer-events:none; z-index:5; }
.watermark span { transform:rotate(-24deg); font-size:min(10vw,96px); font-weight:700; letter-spacing:.06em;
  color:var(--ink); opacity:.06; white-space:nowrap; }
@media print { .watermark span { opacity:.12; } }
"""

STEPS = ("Invoice", "Plan", "Quote", "Approve", "Execute", "Record")
GLYPH = {Action.FUND: "✓", Action.CONVERT: "⇄", Action.DEFER: "◷", Action.ESCALATE: "▲"}


def money(amount: Decimal, ccy: str) -> str:
    return f"{ccy} {amount:,.2f}"


def _uses(plan, d) -> str:
    cur = d.payout_currency
    if d.action is Action.FUND:
        return f"{money(d.pay_amount, cur)} from the {cur} balance"
    if d.action is Action.CONVERT:
        held = f"{money(d.from_balance, cur)} held + " if d.from_balance > 0 else ""
        return f"{held}buy {money(d.convert_buy_amount, cur)} at a locked quote (estimate {money(d.est_sell_amount, d.sell_currency)})"
    if d.action is Action.DEFER and d.receipt_id:
        r = next(r for r in plan.inputs.receipts if r.receipt_id == d.receipt_id)
        return f"Waits for {d.receipt_id}: {money(r.amount, r.currency)} from {r.payer}"
    if d.action is Action.DEFER:
        return "Not due in this planning window"
    return "Nothing prepared"


def _approval(d) -> str:
    return {
        "NEEDS_APPROVAL": "A person approves the exact quote" if d.action is Action.CONVERT else "A person approves the payment",
        "ESCALATED": "A person decides first",
        "NOT_APPLICABLE": "Nothing to approve now",
    }[d.approval.value]


def _card(plan, d, ob, stage_result, prev_plan) -> str:
    cls = ["card"]
    status = ""
    was = ""
    if stage_result is not None:
        if d.obligation_id in stage_result.reused:
            cls.append("reused")
            status = '<span class="recalc">Not recomputed (its inputs did not change)</span>'
        else:
            status = '<span class="recalc">Recomputed</span>'
            if d.obligation_id in stage_result.changed:
                cls.append("changed")
                before = prev_plan.decision(d.obligation_id)
                was = (f'<p class="was">Changed from <strong>{E(before.action.value)}</strong> '
                       f'to <strong>{E(d.action.value)}</strong>.</p>')
            else:
                status = '<span class="recalc">Recomputed, same result</span>'
    limit = ""
    if d.ceiling_level is not None:
        limit = (f'<dt>Limit</dt><dd>{money(d.ceiling_amount, plan.inputs.policy.base_currency)} '
                 f'({E(d.ceiling_level.value.lower())} forecast confidence)</dd>')
    return f"""
<article class="{' '.join(cls)}" aria-label="{E(ob.supplier_name)}">
  <header><h3>{E(ob.supplier_name)}</h3><span class="ref">{E(ob.invoice_ref)} &middot; due {ob.due_at:%b %d %H:%M} UTC</span></header>
  <p class="amount"><span class="ccy">{E(ob.currency)}</span>{ob.amount:,.2f}</p>
  <p class="decision"><span class="chip {d.action.value.lower()}"><span aria-hidden="true">{GLYPH[d.action]}</span> {E(d.action.value)}</span>{status}</p>
  <p class="why">{E(d.reason_text)}</p>
  <dl class="rows"><dt>Plan</dt><dd>{E(_uses(plan, d))}</dd><dt>Approval</dt><dd>{E(_approval(d))}</dd>{limit}</dl>
  {was}
</article>"""


def _stage(i, stage) -> str:
    obs = {o.obligation_id: o for o in stage.plan.inputs.obligations}
    triggers = "".join(
        f'<li><span class="mark" aria-hidden="true">{"+" if i else "="}</span>{E(t)}</li>' for t in stage.trigger)
    cards = "".join(_card(stage.plan, d, obs[d.obligation_id], stage.result, stage.prev) for d in stage.plan.decisions)
    summary = ""
    if stage.result is not None:
        summary = (f'<p class="lede">Recomputed {len(stage.result.recomputed)} of {len(stage.plan.decisions)} obligations; '
                   f'{len(stage.result.reused)} left as they were.</p>')
    return f"""
<section class="panel" aria-labelledby="stage-{i}">
  <h2 id="stage-{i}">{E(stage.title)}</h2>
  <ul class="triggers">{triggers}</ul>
  {summary}
  <div class="cards">{cards}</div>
</section>"""


def render_html(stages) -> str:
    first = stages[0].plan
    bal = "".join(f'<li><span class="ccy">{E(c)}</span>{a:,.2f}</li>' for c, a in sorted(first.inputs.balances.items()))
    current = ' aria-current="step"'
    rail = "".join(f"<li{current if s == 'Plan' else ''}>{s}</li>" for s in STEPS)
    body = "".join(_stage(i, s) for i, s in enumerate(stages))
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Rate-Locked Plan Card</title>
<meta name="description" content="Prototype plan card: three obligations, fund, convert, defer or escalate, with only the affected decisions recomputed.">
<style>{TOKENS_CSS}{BASE_CSS}</style>
</head>
<body>
<a class="skip" href="#main">Skip to the plan</a>
<div class="topbar" role="banner">
  <span class="brand">Rate-Locked Approval</span>
  <span class="chip">WORLD: SIMULATED</span>
  <span class="chip">RUN: LOCAL FAKE</span>
  <span class="chip">READER: RULE-BASED</span>
  <span class="chip">Test money</span>
</div>
<div class="layout">
  <nav class="rail" aria-label="Steps"><ol>{rail}</ol></nav>
  <main id="main">
    <h1>Pay what we can, convert only at an approved quote</h1>
    <p class="lede">Code decides for each obligation: fund it, convert for it, defer it or escalate it. Nothing here moves money. Executing a conversion needs a person to approve the exact quote.</p>
    <section class="panel" aria-labelledby="bal"><h2 id="bal">Balances</h2>
      <ul class="balances">{bal}</ul></section>
    {body}
  </main>
</div>
<footer>
  <p>Execution limits and the planning window are demo policy, not an Airwallex setting. Suppliers, receipts and amounts are fictional test fixtures.</p>
  <p>{E(NOTICE)}</p>
</footer>
<div class="watermark" aria-hidden="true"><span>PROTOTYPE, NOT WIRED</span></div>
</body>
</html>
"""

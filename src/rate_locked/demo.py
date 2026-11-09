"""Offline end-to-end story on the local fake: no keys, no network. `python -m rate_locked demo`.

Every number printed comes from this run. The world is SIMULATED (the local fake), not the Airwallex sandbox.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, replace
from decimal import Decimal as D
from pathlib import Path
from typing import Callable, Optional

from .approval import ApprovalGate
from .awx.fake import FakeAirwallex
from .clock import ManualClock
from .executor import Executor, State
from .models import Confidence
from .orchestrate import refresh_balances, snapshot_inputs
from .planner import Plan, make_plan, replan
from .record import verify_record
from .scenario import demo_inputs


@dataclass
class Stage:
    title: str
    trigger: list           # human lines: what changed
    plan: Plan
    result: object = None   # ReplanResult for stages after the first
    prev: Optional[Plan] = None


@dataclass
class DemoResult:
    stages: list
    transcript: list
    record: dict
    problems_good: list
    problems_tampered: list
    stale_counts: dict
    final_counts: dict
    difference_text: str


def run_demo(say: Callable[[str], None] = lambda s: None) -> DemoResult:
    clock = ManualClock()
    fake = FakeAirwallex.demo(clock=clock)
    gate = ApprovalGate(clock)
    ex = Executor(fake, gate, clock)
    out = []

    def log(line):
        out.append(line)
        say(line)

    seed = demo_inputs()
    obligations = []
    for i, o in enumerate(seed.obligations):
        b = fake.create_beneficiary(request_id=f"demo-ben-{i}", payload={"name": o.supplier_name})
        obligations.append(replace(o, beneficiary_id=b.beneficiary_id))
    inputs = snapshot_inputs(fake, obligations, seed.receipts, seed.policy, clock.now())

    plan1 = make_plan(inputs)
    log("Plan v1: " + ", ".join(f"{d.obligation_id} {d.action.value}" for d in plan1.decisions))
    stages = [Stage("Plan v1: baseline", ["Balances and indicative rates read from the sandbox world (simulated here)."], plan1)]

    inputs_a = inputs.with_receipt_confidence("R1", Confidence.LOW)
    res2 = replan(plan1, inputs_a)
    log(f"Receipt R1 confidence HIGH -> LOW. Recomputed {list(res2.recomputed)}, reused {list(res2.reused)}")
    stages.append(Stage("Plan v2: receipt confidence lowered",
                        ["Receipt R1 (Vandelay Industries, EUR 1,800.00): confidence HIGH to LOW",
                         "Execution limit follows forecast confidence: USD 5,000.00 to USD 2,000.00 (demo policy)"],
                        res2.plan, res2, plan1))

    dep = fake.simulate_deposit(request_id="demo-dep-1", currency="USD", amount=D("1000"))
    inputs_b = refresh_balances(fake, inputs_a)
    res3 = replan(res2.plan, inputs_b)
    log(f"Simulated deposit {dep.currency} {dep.amount}. Recomputed {list(res3.recomputed)}, reused {list(res3.reused)}")
    stages.append(Stage("Plan v3: deposit simulated",
                        [f"Simulated deposit: {dep.currency} +{dep.amount:,.2f} (sandbox simulation endpoint)"],
                        res3.plan, res3, res2.plan))

    plan = res3.plan
    decision, ob = plan.decision("O2"), next(o for o in obligations if o.obligation_id == "O2")

    prep = ex.prepare(decision, ob, plan.digest, validity="MIN_1")
    q1 = prep.execution.envelope.quote
    log(f"Quote {q1.quote_id} locked: sell {q1.sell_currency} {q1.sell_amount} for {q1.buy_currency} {q1.buy_amount} at {q1.client_rate}")
    clock.advance(61)
    stale = ex.approve(prep.token, prep.execution.envelope.digest, "approver-1")
    stale_counts = ex.call_counts(prep.execution.execution_id)
    log(f"Approval after expiry refused ({stale.code}). Conversion calls: {stale_counts['conversion_create']}. "
        f"Payout calls: {stale_counts['transfer_create']}.")

    fake.set_rate("GBPUSD", "1.31")
    prep2, diff = ex.requote(prep.execution.execution_id)
    log(f"New quote {prep2.execution.envelope.quote.quote_id}. {diff.text} ({diff.old_sell} -> {diff.new_sell})")

    ok = ex.approve(prep2.token, prep2.execution.envelope.digest, "approver-1")
    again = ex.approve(prep2.token, prep2.execution.envelope.digest, "approver-1")
    log(f"Approved: {ok.ok}. Second use of the link: {again.code}")
    done = ex.dispatch(prep2.execution.execution_id)
    fake.simulate_transfer_transition(done.transfer.transfer_id, "PAID")
    done = ex.advance(done.execution_id)
    counts = ex.call_counts(done.execution_id)
    log(f"State {done.state.value}. Conversion calls: {counts['conversion_create']}. Payout calls: {counts['transfer_create']}.")

    rec = ex.record(done.execution_id).to_dict()
    good = verify_record(rec, expected_envelope_digest=done.envelope.digest)
    bad = json.loads(json.dumps(rec))
    next(r for r in bad["rows"] if r["field"] == "rate")["executed"] = "1.20"
    problems = verify_record(bad, expected_envelope_digest=done.envelope.digest)
    log(f"Verify good copy: {'PASS' if not good else 'FAIL'}. Verify edited copy: {next((p for p in problems if '_MISMATCH at row' in p), problems[0] if problems else 'PASS')}")
    assert done.state is State.PAID
    return DemoResult(stages, out, rec, good, problems, stale_counts, counts, diff.text)


def main(out_dir: Optional[Path] = None) -> DemoResult:
    from .plancard import render_html
    result = run_demo(print)
    out_dir = Path(out_dir or "prototype")
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "plan_card.html").write_text(render_html(result.stages), encoding="utf-8", newline="\n")
    (out_dir / "demo_record.json").write_text(json.dumps(result.record, indent=2, sort_keys=True), encoding="utf-8", newline="\n")
    print(f"Wrote {out_dir / 'plan_card.html'} and {out_dir / 'demo_record.json'}")
    return result

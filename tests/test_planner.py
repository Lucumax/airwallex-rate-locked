import random
from dataclasses import fields, replace
from datetime import timedelta
from decimal import Decimal as D

from rate_locked.models import Action, ApprovalNeed, Confidence, Obligation, Receipt
from rate_locked.planner import PlanInputs, Policy, RateBook, ceiling_level, make_plan, replan
from rate_locked.scenario import NOW, demo_inputs


def h(n):
    return NOW + timedelta(hours=n)


def by_id(plan):
    return {d.obligation_id: d for d in plan.decisions}


# ---- v1 plan on the demo scenario ------------------------------------------------------

def test_demo_plan_v1_fund_convert_defer():
    plan = make_plan(demo_inputs())
    d = by_id(plan)
    assert d["O1"].action is Action.FUND
    assert d["O2"].action is Action.CONVERT
    assert d["O3"].action is Action.DEFER
    assert d["O3"].receipt_id == "R1"


def test_demo_convert_uses_buffered_estimate_and_buys_only_the_shortfall():
    d = by_id(make_plan(demo_inputs()))["O2"]
    assert d.convert_currency == "GBP"
    assert d.convert_buy_amount == D("1000.00")
    assert d.sell_currency == "USD"
    assert d.est_sell_amount == D("1306.50")  # 1000 * 1.30 * 1.005


def test_convert_uses_leftover_balance_and_converts_only_what_is_short():
    inp = demo_inputs()
    inp = replace(inp, balances={"USD": D("5000"), "EUR": D("700"), "GBP": D("0")}, receipts=())
    inp = replace(inp, obligations=(inp.obligations[0],))  # O1 EUR 1200, have EUR 700
    d = by_id(make_plan(inp))["O1"]
    assert d.action is Action.CONVERT
    assert d.from_balance == D("700")
    assert d.convert_buy_amount == D("500.00")


def test_fund_includes_payout_fee_in_the_need():
    ob = Obligation("A", "S", "b", D("100"), "EUR", h(5), payout_fee=D("12.85"), payout_method="SWIFT")
    inp = replace(demo_inputs(), obligations=(ob,), balances={"USD": D("0"), "EUR": D("110")}, receipts=())
    d = by_id(make_plan(inp))["A"]
    assert d.action is Action.ESCALATE  # 110 < 112.85 and no USD to convert
    inp2 = replace(inp, balances={"USD": D("0"), "EUR": D("112.85")})
    assert by_id(make_plan(inp2))["A"].action is Action.FUND


# ---- approval need -------------------------------------------------------------------------

def test_approval_need_is_never_dropped_for_executable_actions():
    plan = make_plan(demo_inputs())
    for d in plan.decisions:
        if d.action in (Action.FUND, Action.CONVERT):
            assert d.approval is ApprovalNeed.NEEDS_APPROVAL
        elif d.action is Action.ESCALATE:
            assert d.approval is ApprovalNeed.ESCALATED
        else:
            assert d.approval is ApprovalNeed.NOT_APPLICABLE


# ---- forecast-confidence ceiling ----------------------------------------------------------

def test_ceiling_level_high_only_when_all_receipts_in_horizon_are_high():
    inp = demo_inputs()
    assert ceiling_level(inp) is Confidence.HIGH
    low = replace(inp, receipts=(replace(inp.receipts[0], confidence=Confidence.LOW),))
    assert ceiling_level(low) is Confidence.LOW
    none = replace(inp, receipts=())
    assert ceiling_level(none) is Confidence.LOW
    outside = replace(inp, receipts=(replace(inp.receipts[0], expected_at=h(200)),))
    assert ceiling_level(outside) is Confidence.LOW  # nothing in horizon supports a higher limit


def test_conversion_above_ceiling_escalates():
    inp = demo_inputs()
    big = Obligation("B", "Big Supplier", "b", D("4000"), "GBP", h(10))
    inp = replace(inp, obligations=(big,), balances={"USD": D("9000"), "GBP": D("0")}, receipts=())
    d = by_id(make_plan(inp))["B"]
    assert d.action is Action.ESCALATE
    assert d.reason_code == "CEILING_EXCEEDED"
    assert d.approval is ApprovalNeed.ESCALATED
    assert d.ceiling_level is Confidence.LOW


def test_high_confidence_raises_the_ceiling_but_still_needs_approval():
    inp = demo_inputs()
    ob = Obligation("B", "Big Supplier", "b", D("2500"), "GBP", h(10))  # cost about 3266 USD
    base = replace(inp, obligations=(ob,), balances={"USD": D("9000"), "GBP": D("0")})
    low = replace(base, receipts=())
    assert by_id(make_plan(low))["B"].action is Action.ESCALATE
    d = by_id(make_plan(base))["B"]  # R1 HIGH in horizon
    assert d.action is Action.CONVERT
    assert d.approval is ApprovalNeed.NEEDS_APPROVAL


# ---- defer rules ----------------------------------------------------------------------------

def test_defer_needs_high_confidence_receipt_that_arrives_before_due_minus_lead():
    inp = demo_inputs()
    r = inp.receipts[0]
    late = replace(inp, receipts=(replace(r, expected_at=h(55)),))  # after due-12h (48h)
    assert by_id(make_plan(late))["O3"].action is Action.ESCALATE
    low = replace(inp, receipts=(replace(r, confidence=Confidence.LOW),))
    d = by_id(make_plan(low))["O3"]
    assert d.action is Action.ESCALATE and d.reason_code == "CASH_SHORT"
    small = replace(inp, receipts=(replace(r, amount=D("1000")),))
    assert by_id(make_plan(small))["O3"].action is Action.ESCALATE


def test_overdue_receipt_does_not_support_a_deferral():
    inp = demo_inputs()
    r = replace(inp.receipts[0], expected_at=h(-3))
    assert by_id(make_plan(replace(inp, receipts=(r,))))["O3"].action is Action.ESCALATE


def test_not_due_in_horizon_is_deferred_without_reserving_anything():
    inp = demo_inputs()
    far = Obligation("F", "Far Supplier", "b", D("500"), "EUR", h(200))
    d = by_id(make_plan(replace(inp, obligations=(far,))))["F"]
    assert d.action is Action.DEFER and d.reason_code == "NOT_DUE_IN_HORIZON" and d.reserves == ()


def test_one_receipt_cannot_cover_two_deferrals():
    inp = demo_inputs()
    a = Obligation("A", "A", "b", D("1000"), "EUR", h(60))
    b = Obligation("B", "B", "b", D("1000"), "EUR", h(61))
    inp = replace(inp, obligations=(a, b), balances={"USD": D("0"), "EUR": D("0")},
                  receipts=(replace(inp.receipts[0], amount=D("1500")),))
    d = by_id(make_plan(inp))
    assert d["A"].action is Action.DEFER
    assert d["B"].action is Action.ESCALATE


# ---- reservation, ordering, edge cases ---------------------------------------------------

def test_earlier_due_obligation_gets_the_cash_first():
    inp = demo_inputs()
    a = Obligation("LATE", "L", "b", D("900"), "EUR", h(40))
    b = Obligation("EARLY", "E", "b", D("900"), "EUR", h(10))
    inp = replace(inp, obligations=(a, b), balances={"USD": D("0"), "EUR": D("1000")}, receipts=())
    plan = make_plan(inp)
    assert [d.obligation_id for d in plan.decisions] == ["EARLY", "LATE"]
    d = by_id(plan)
    assert d["EARLY"].action is Action.FUND and d["LATE"].action is Action.ESCALATE


def test_base_currency_shortage_cannot_be_converted():
    inp = demo_inputs()
    ob = Obligation("U", "U", "b", D("500"), "USD", h(10))
    d = by_id(make_plan(replace(inp, obligations=(ob,), balances={"USD": D("100")}, receipts=())))["U"]
    assert d.action is Action.ESCALATE and d.reason_code == "CASH_SHORT"


def test_missing_rate_escalates_with_a_reason():
    inp = demo_inputs()
    ob = Obligation("J", "J", "b", D("500"), "JPY", h(10))
    d = by_id(make_plan(replace(inp, obligations=(ob,), receipts=())))["J"]
    assert d.action is Action.ESCALATE and d.reason_code == "NO_RATE"


def test_planner_is_deterministic_and_pure():
    inp = demo_inputs()
    before = repr(inp)
    p1, p2 = make_plan(inp), make_plan(inp)
    assert p1.digest == p2.digest and repr(inp) == before


def test_planner_inputs_carry_no_model_field():
    names = {f.name for f in fields(PlanInputs)}
    assert names == {"now", "obligations", "balances", "receipts", "rates", "policy"}


# ---- replan: only affected obligations -----------------------------------------------------

def test_lowering_receipt_confidence_recomputes_only_dependents():
    inp = demo_inputs()
    v1 = make_plan(inp)
    res = replan(v1, inp.with_receipt_confidence("R1", Confidence.LOW))
    assert res.reused == ("O1",)
    assert set(res.recomputed) == {"O2", "O3"}
    d = by_id(res.plan)
    assert d["O1"] is by_id(v1)["O1"]  # literally the same decision object
    assert d["O2"].action is Action.CONVERT and d["O2"].ceiling_level is Confidence.LOW
    assert d["O3"].action is Action.ESCALATE and d["O3"].reason_code == "CASH_SHORT"
    assert "O3" in res.changed and "O2" not in res.changed


def test_simulated_deposit_flips_the_escalation_to_a_conversion():
    inp = demo_inputs().with_receipt_confidence("R1", Confidence.LOW)
    v2 = make_plan(inp)
    res = replan(v2, inp.with_deposit("USD", D("1000")))
    assert res.reused == ("O1",)
    d = by_id(res.plan)
    assert d["O3"].action is Action.CONVERT
    assert d["O3"].convert_buy_amount == D("1700.00")
    assert res.plan.version == v2.version + 1


def test_deposit_in_unrelated_currency_recomputes_nothing():
    inp = demo_inputs()
    v1 = make_plan(inp)
    res = replan(v1, inp.with_deposit("CAD", D("50")))
    assert res.recomputed == () and set(res.reused) == {"O1", "O2", "O3"}
    assert res.changed == ()


def test_replan_matches_a_full_recompute_on_random_changes():
    rng = random.Random(20261027)
    currencies = ["EUR", "GBP", "SGD"]
    rates = RateBook({("USD", c): D(str(rng.choice([0.7, 1.08, 1.3]))) for c in currencies})
    for case in range(300):
        obs = tuple(
            Obligation(f"O{i}", f"S{i}", f"b{i}", D(rng.randint(100, 4000)), rng.choice(currencies),
                       h(rng.randint(1, 90)), payout_fee=D(rng.choice(["0", "0", "12.85"])))
            for i in range(rng.randint(1, 5)))
        recs = tuple(
            Receipt(f"R{i}", "P", D(rng.randint(200, 3000)), rng.choice(currencies), h(rng.randint(-5, 80)),
                    rng.choice(list(Confidence)))
            for i in range(rng.randint(0, 3)))
        bal = {c: D(rng.randint(0, 3000)) for c in currencies + ["USD"]}
        inp = PlanInputs(NOW, obs, bal, recs, rates, Policy())
        prev = make_plan(inp)
        new = inp
        for _ in range(rng.randint(1, 3)):
            kind = rng.choice(["dep", "conf", "bal", "rec_amt", "rate"])
            if kind == "dep":
                new = new.with_deposit(rng.choice(currencies + ["USD"]), D(rng.randint(1, 2500)))
            elif kind == "conf" and new.receipts:
                r = rng.choice(new.receipts)
                new = new.with_receipt_confidence(r.receipt_id, rng.choice(list(Confidence)))
            elif kind == "rec_amt" and new.receipts:
                r = rng.choice(new.receipts)
                new = replace(new, receipts=tuple(replace(x, amount=D(rng.randint(100, 3500))) if x is r else x
                                                 for x in new.receipts))
            elif kind == "rate":
                new = replace(new, rates=RateBook({**new.rates.sell_per_buy,
                                                  ("USD", rng.choice(currencies)): D(str(rng.choice([0.7, 0.9, 1.08, 1.3, 1.6])))}))
            else:
                new = replace(new, balances={**new.balances, rng.choice(currencies): D(rng.randint(0, 3000))})
        res = replan(prev, new)
        full = make_plan(new)
        key = lambda p: [(d.obligation_id, d.action, d.reason_code, d.convert_buy_amount, d.receipt_id, d.reserves)
                         for d in p.decisions]
        assert key(res.plan) == key(full), f"case {case}"
        assert set(res.reused) | set(res.recomputed) == {o.obligation_id for o in obs}


def test_replan_with_changed_obligations_recomputes_everything():
    inp = demo_inputs()
    v1 = make_plan(inp)
    changed = replace(inp, obligations=inp.obligations[:2])
    res = replan(v1, changed)
    assert res.reused == () and set(res.recomputed) == {"O1", "O2"}


def test_every_decision_lists_what_it_read():
    d = by_id(make_plan(demo_inputs()))
    assert "balance:EUR" in d["O1"].reads and "policy:ceiling" not in d["O1"].reads
    assert "policy:ceiling" in d["O2"].reads and "rate:USD>GBP" in d["O2"].reads
    assert "receipt:R1" in d["O3"].reads and "receipts:EUR" in d["O3"].reads

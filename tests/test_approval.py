from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D

import pytest

from rate_locked.approval import (ApprovalError, ApprovalGate, EnvelopeError, build_envelope)
from rate_locked.awx.fake import FakeAirwallex
from rate_locked.clock import ManualClock
from rate_locked.models import Action, Confidence
from rate_locked.planner import make_plan
from rate_locked.scenario import demo_inputs

MARGIN = timedelta(seconds=10)


def setup():
    clock = ManualClock()
    fake = FakeAirwallex.demo(clock=clock)
    plan = make_plan(demo_inputs())
    ob = next(o for o in plan.inputs.obligations if o.obligation_id == "O2")
    dec = plan.decision("O2")
    quote = fake.create_quote(buy_currency="GBP", sell_currency="USD", validity="MIN_1", buy_amount=D("1000"))
    return clock, fake, plan, ob, dec, quote


def envelope(**kw):
    clock, fake, plan, ob, dec, quote = setup()
    return clock, fake, quote, build_envelope(dec, ob, quote, now=clock.now(), plan_digest=plan.digest, **kw)


# ---- envelope ------------------------------------------------------------------------------------

def test_envelope_binds_quote_and_pre_minted_request_ids():
    _c, _f, quote, env = envelope()
    assert env.quote == quote and env.mode is Action.CONVERT
    assert env.conversion_request_id and env.transfer_request_id
    assert env.conversion_request_id != env.transfer_request_id
    assert env.payout_amount == D("1000.00") and env.payout_currency == "GBP"


def test_expiry_is_min_of_quote_minus_margin_and_ttl():
    clock, _f, quote, env = envelope()  # MIN_1 quote, ttl 10 min -> quote is the tighter bound
    assert env.expires_at == quote.valid_to_at - MARGIN
    clock2, _f2, _q2, env2 = envelope(ttl=timedelta(seconds=20))
    assert env2.expires_at == clock2.now() + timedelta(seconds=20)


@pytest.mark.parametrize("field,change", [
    ("quote_id", lambda q: replace(q, quote_id="other")),
    ("client_rate", lambda q: replace(q, client_rate=D("1.31"))),
    ("sell_amount", lambda q: replace(q, sell_amount=q.sell_amount + D("0.01"))),
    ("buy_amount", lambda q: replace(q, buy_amount=q.buy_amount + D("0.01"))),
    ("valid_to_at", lambda q: replace(q, valid_to_at=q.valid_to_at + timedelta(seconds=1))),
    ("sell_currency", lambda q: replace(q, sell_currency="EUR")),
])
def test_digest_changes_when_any_quote_field_changes(field, change):
    _c, _f, quote, env = envelope()
    changed = replace(env, quote=change(quote))
    assert changed.digest != env.digest


@pytest.mark.parametrize("field,value", [
    ("beneficiary_id", "someone-else"), ("payout_amount", D("1000.01")), ("payout_fee", D("1")),
    ("conversion_request_id", "x"), ("transfer_request_id", "y"), ("supplier_name", "Other Ltd"),
    ("payout_currency", "EUR"), ("invoice_ref", "INV-9"), ("plan_digest", "deadbeef"),
    ("existing_balance_used", D("1")),
])
def test_digest_changes_when_any_bound_term_changes(field, value):
    _c, _f, _q, env = envelope()
    assert replace(env, **{field: value}).digest != env.digest


def test_digest_is_stable_across_identical_envelopes():
    _c, _f, _q, env = envelope()
    assert replace(env).digest == env.digest


def test_cannot_envelope_a_deferred_or_escalated_decision():
    clock, fake, plan, ob, _dec, quote = setup()
    inp = demo_inputs().with_receipt_confidence("R1", Confidence.LOW)
    esc = make_plan(inp).decision("O3")
    assert esc.action is Action.ESCALATE
    with pytest.raises(EnvelopeError) as e:
        build_envelope(esc, ob, quote, now=clock.now(), plan_digest="d")
    assert e.value.code == "NOT_EXECUTABLE"
    deferred = make_plan(demo_inputs()).decision("O3")
    assert deferred.action is Action.DEFER
    with pytest.raises(EnvelopeError):
        build_envelope(deferred, ob, quote, now=clock.now(), plan_digest="d")


def test_envelope_rejects_a_quote_that_does_not_match_the_plan():
    clock, fake, plan, ob, dec, quote = setup()
    wrong_ccy = fake.create_quote(buy_currency="EUR", sell_currency="USD", validity="MIN_1", buy_amount=D("1000"))
    with pytest.raises(EnvelopeError) as e:
        build_envelope(dec, ob, wrong_ccy, now=clock.now(), plan_digest="d")
    assert e.value.code == "QUOTE_MISMATCH"
    small = fake.create_quote(buy_currency="GBP", sell_currency="USD", validity="MIN_1", buy_amount=D("900"))
    with pytest.raises(EnvelopeError) as e:
        build_envelope(dec, ob, small, now=clock.now(), plan_digest="d")
    assert e.value.code == "QUOTE_MISMATCH"


def test_envelope_refuses_a_convert_without_a_quote_and_a_fund_with_one():
    clock, fake, plan, ob, dec, quote = setup()
    with pytest.raises(EnvelopeError):
        build_envelope(dec, ob, None, now=clock.now(), plan_digest="d")
    fund = plan.decision("O1")
    with pytest.raises(EnvelopeError):
        build_envelope(fund, plan.inputs.obligations[0], quote, now=clock.now(), plan_digest="d")


def test_envelope_refuses_a_quote_too_close_to_expiry():
    clock, fake, plan, ob, dec, quote = setup()
    clock.advance(55)
    with pytest.raises(EnvelopeError) as e:
        build_envelope(dec, ob, quote, now=clock.now(), plan_digest="d")
    assert e.value.code == "TOO_CLOSE_TO_EXPIRY"


def test_fund_envelope_has_no_quote_and_no_conversion_request():
    clock, fake, plan, _ob, _dec, _q = setup()
    ob1 = plan.inputs.obligations[0]
    env = build_envelope(plan.decision("O1"), ob1, None, now=clock.now(), plan_digest=plan.digest)
    assert env.mode is Action.FUND and env.quote is None and env.conversion_request_id is None
    assert env.expires_at == clock.now() + timedelta(minutes=10)


# ---- gate ----------------------------------------------------------------------------------------

def test_approval_is_single_use():
    clock, _f, _q, env = envelope()
    gate = ApprovalGate(clock)
    token = gate.issue(env)
    a = gate.approve(token, env.digest, "approver-1")
    assert a.envelope_digest == env.digest and a.approver_id == "approver-1"
    with pytest.raises(ApprovalError) as e:
        gate.approve(token, env.digest, "approver-1")
    assert e.value.code == "NONCE_USED"


def test_presented_digest_must_match_and_a_mismatch_burns_the_token():
    clock, _f, _q, env = envelope()
    gate = ApprovalGate(clock)
    token = gate.issue(env)
    with pytest.raises(ApprovalError) as e:
        gate.approve(token, "0" * 64, "approver-1")
    assert e.value.code == "TERMS_CHANGED"
    with pytest.raises(ApprovalError) as e2:
        gate.approve(token, env.digest, "approver-1")
    assert e2.value.code == "NONCE_USED"


def test_approval_after_expiry_is_refused():
    clock, _f, _q, env = envelope()
    gate = ApprovalGate(clock)
    token = gate.issue(env)
    clock.advance(51)  # past valid_to - margin
    with pytest.raises(ApprovalError) as e:
        gate.approve(token, env.digest, "approver-1")
    assert e.value.code == "EXPIRED"


def test_unknown_token_and_deny():
    clock, _f, _q, env = envelope()
    gate = ApprovalGate(clock)
    with pytest.raises(ApprovalError) as e:
        gate.approve("nope", env.digest, "a")
    assert e.value.code == "UNKNOWN_TOKEN"
    token = gate.issue(env)
    gate.deny(token, "approver-1")
    with pytest.raises(ApprovalError) as e2:
        gate.approve(token, env.digest, "approver-1")
    assert e2.value.code == "NONCE_USED"


def test_tokens_are_not_stored_in_clear():
    clock, _f, _q, env = envelope()
    gate = ApprovalGate(clock)
    token = gate.issue(env)
    assert token not in repr(vars(gate)) and len(token) >= 32


def test_authorize_is_one_shot_and_needs_a_prior_approval():
    clock, fake, quote, env = envelope()
    gate = ApprovalGate(clock)
    token = gate.issue(env)
    with pytest.raises(ApprovalError) as e:
        gate.authorize(env.envelope_id, fake.get_quote(quote.quote_id), env.digest)
    assert e.value.code == "NOT_APPROVED"
    gate.approve(token, env.digest, "approver-1")
    gate.authorize(env.envelope_id, fake.get_quote(quote.quote_id), env.digest)
    with pytest.raises(ApprovalError) as e2:
        gate.authorize(env.envelope_id, fake.get_quote(quote.quote_id), env.digest)
    assert e2.value.code == "ALREADY_DISPATCHED"


def test_authorize_refuses_after_expiry_and_when_the_live_quote_differs():
    clock, fake, quote, env = envelope()
    gate = ApprovalGate(clock)
    gate.approve(gate.issue(env), env.digest, "approver-1")
    clock.advance(55)
    with pytest.raises(ApprovalError) as e:
        gate.authorize(env.envelope_id, quote, env.digest)
    assert e.value.code == "EXPIRED"
    clock2, fake2, quote2, env2 = envelope()
    gate2 = ApprovalGate(clock2)
    gate2.approve(gate2.issue(env2), env2.digest, "approver-1")
    with pytest.raises(ApprovalError) as e2:
        gate2.authorize(env2.envelope_id, replace(quote2, client_rate=D("1.31")), env2.digest)
    assert e2.value.code == "TERMS_CHANGED"

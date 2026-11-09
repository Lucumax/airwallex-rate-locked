"""Regression tests for the first independent review (findings F1 to F16). Each test names its finding."""
import copy
import threading
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D
from types import MappingProxyType

import pytest

from rate_locked import approval as approval_mod
from rate_locked.approval import ApprovalError, ApprovalGate, build_envelope
from rate_locked.awx import wire
from rate_locked.awx.adapter import AirwallexError, UncertainOutcome
from rate_locked.awx.http_adapter import HttpAdapter
from rate_locked.executor import EventLog, State
from rate_locked.models import Confidence, Obligation, Receipt
from rate_locked.planner import PlanInputs, Policy, RateBook, make_plan, replan
from rate_locked.record import build_record, verify_record
from rate_locked.scenario import NOW, demo_inputs

from helpers import World


def h(n):
    return NOW + timedelta(hours=n)


# ---- F1: dispatch executes exactly what was approved ---------------------------------------------

def test_f1_envelope_cannot_be_reassigned():
    w = World()
    ex = w.approved("O1")
    with pytest.raises(AttributeError):
        ex.envelope = replace(ex.envelope, payout_amount=D("1"))


def test_f1_tampered_envelope_object_voids_dispatch_with_zero_calls():
    w = World()
    ex = w.approved("O1")
    ex._envelope = replace(ex._envelope, payout_amount=D("1"))
    ex = w.executor.dispatch(ex.execution_id)
    assert ex.state is State.VOIDED
    assert w.executor.call_counts(ex.execution_id) == {"conversion_create": 0, "transfer_create": 0}


# ---- F2: concurrent progress calls send once -----------------------------------------------------

def test_f2_concurrent_dispatch_and_advance_send_one_transfer():
    w = World()
    ex = w.approved("O1")
    entered, release = threading.Event(), threading.Event()
    orig = w.fake.get_balances

    def slow():
        entered.set()
        release.wait(3)
        return orig()

    w.fake.get_balances = slow
    results = []
    threads = [threading.Thread(target=lambda f=f: results.append(f(ex.execution_id)))
               for f in (w.executor.dispatch, w.executor.dispatch, w.executor.advance)]
    threads[0].start()
    assert entered.wait(3)
    threads[1].start()
    threads[2].start()
    threading.Timer(0.3, release.set).start()
    for t in threads:
        t.join(10)
    assert w.executor.call_counts(ex.execution_id) == {"conversion_create": 0, "transfer_create": 1}
    assert ex.state is State.PAYOUT_REPORTED


# ---- F3: the deadline is enforced immediately before the send --------------------------------------

def test_f3_approval_expiring_during_preparatory_io_prevents_the_send():
    w = World()
    ex = w.approved("O1")
    orig = w.fake.get_balances

    def slow():
        w.clock.advance(601)
        return orig()

    w.fake.get_balances = slow
    ex = w.executor.dispatch(ex.execution_id)
    assert ex.state is State.EXPIRED
    assert w.executor.call_counts(ex.execution_id) == {"conversion_create": 0, "transfer_create": 0}


def test_f3_slow_intent_logging_cannot_extend_the_deadline():
    w = World()

    class SlowLog(EventLog):
        def append(self, execution_id, at, kind, data):
            if kind in ("conversion_dispatching", "transfer_dispatching"):
                w.clock.advance(55)
            return super().append(execution_id, at, kind, data)

    w.executor.log = SlowLog()
    ex = w.approved("O2")
    ex = w.executor.dispatch(ex.execution_id)
    assert ex.state is State.EXPIRED
    assert w.executor.call_counts(ex.execution_id) == {"conversion_create": 0, "transfer_create": 0}
    assert "dispatch_aborted_before_send" in [e.kind for e in ex.events]
    prep, _diff = w.executor.requote(ex.execution_id)  # nothing was sent, so asking again is allowed
    assert prep.token


def test_f3_quote_not_yet_valid_is_refused():
    w = World()
    prep = w.prepare("O2")
    env = prep.execution.envelope
    gate = ApprovalGate(w.clock)
    future = replace(env.quote, valid_from_at=w.clock.now() + timedelta(seconds=5))
    env2 = replace(env, quote=future, envelope_id="future-1")
    gate.approve(gate.issue(env2), env2.digest, "a")
    with pytest.raises(ApprovalError) as e:
        gate.authorize("future-1", future, env2.digest)
    assert e.value.code == "NOT_YET_VALID"


def test_f3_write_ahead_intent_is_logged_before_the_adapter_call():
    w = World()
    seen = {}

    class Probe(EventLog):
        def append(self, execution_id, at, kind, data):
            if kind in ("conversion_dispatching", "transfer_dispatching"):
                seen[kind] = (w.executor.calls.count("create_conversion"), w.executor.calls.count("create_transfer"))
            return super().append(execution_id, at, kind, data)

    w.executor.log = Probe()
    w.executor.dispatch(w.approved("O2").execution_id)
    assert seen["conversion_dispatching"] == (0, 0)  # no financial call had been made when the intent was written
    assert seen["transfer_dispatching"] == (1, 0)


# ---- F4: unusable responses and unexpected errors are uncertain, and counted ---------------------------

def test_f4_unexpected_exception_on_a_create_is_uncertain_and_counted():
    w = World()
    ex = w.approved("O2")

    def boom(**kw):
        raise ValueError("malformed")

    w.fake.create_conversion = boom
    ex = w.executor.dispatch(ex.execution_id)
    assert ex.state is State.UNKNOWN and ex.unknown_stage == "conversion"
    assert w.executor.call_counts(ex.execution_id)["conversion_create"] == 1


class _Resp:
    def __init__(self, status, body):
        self.status, self._body = status, body
        self.headers = {}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Opener:
    def __init__(self, conv_body, quote_body=b"{not json"):
        self.conv_body, self.quote_body = conv_body, quote_body

    def open(self, req, timeout=None):
        url = req.full_url
        if url.endswith("/authentication/login"):
            return _Resp(201, b'{"token": "t", "expires_at": "2999-01-01T00:00:00+0000"}')
        if url.endswith("/fx/conversions/create"):
            return _Resp(201, self.conv_body)
        return _Resp(200, self.quote_body)


@pytest.mark.parametrize("body", [b"{not json", b"\xff\xfe", b'{"conversion_id": "x"}'])
def test_f4_malformed_successful_create_response_is_uncertain(body):
    a = HttpAdapter("http://127.0.0.1:1", "id", "key")
    a._opener = _Opener(body)
    with pytest.raises(UncertainOutcome):
        a.create_conversion(request_id="r", quote_id="q", buy_currency="GBP", sell_currency="USD", buy_amount=D("1"))


def test_f4_malformed_read_response_is_a_provider_error_not_a_crash():
    a = HttpAdapter("http://127.0.0.1:1", "id", "key")
    a._opener = _Opener(b"", quote_body=b"{not json")
    with pytest.raises(AirwallexError) as e:
        a.get_quote("q")
    assert e.value.code == "malformed_response"


# ---- F5: an envelope cannot be re-issued to reset its authorization --------------------------------

def test_f5_reissuing_an_envelope_is_refused():
    w = World()
    env = w.prepare("O2").execution.envelope
    with pytest.raises(ValueError):
        w.gate.issue(env)


# ---- F6: record covers currencies and derived flags -------------------------------------------------

def run_fund(w):
    return w.executor.dispatch(w.approved("O1").execution_id)


def test_f6_payout_currency_mismatch_is_flagged():
    w = World()
    ex = run_fund(w)
    ex.transfer = replace(ex.transfer, transfer_currency="USD")
    rec = build_record(ex, {}, w.clock.now())
    assert not rec.all_match
    assert next(r for r in rec.rows if r.field == "payout_currency").matches is False


def test_f6_verifier_recomputes_derived_flags():
    w = World()
    ex = run_fund(w)
    d = build_record(ex, {}, w.clock.now()).to_dict()
    bad = copy.deepcopy(d)
    bad["rows"][0]["matches"] = False
    bad["all_match"] = False
    bad["record_digest"] = wire_digest(bad)
    assert any("MATCHES" in p or "ALL_MATCH" in p for p in verify_record(bad))
    bad2 = copy.deepcopy(d)
    bad2["complete"] = False
    bad2["record_digest"] = wire_digest(bad2)
    assert any("COMPLETE" in p for p in verify_record(bad2))


def wire_digest(d):
    from rate_locked.record import record_digest_of
    return record_digest_of(d)


def test_f6_identifiers_compare_as_strings_not_numbers():
    from rate_locked.record import _same_field
    assert _same_field("beneficiary", "1e3", "1000") is False
    assert _same_field("payout_amount", "1000", "1000.00") is True


# ---- F7: recovery claim is in-process only ------------------------------------------------------------

def test_f7_readme_states_recovery_is_in_memory_only():
    from pathlib import Path
    text = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
    assert "in memory" in text.lower() and "restart" in text.lower()


# ---- F8, F9, F10: planner ----------------------------------------------------------------------------

def test_f8_inputs_are_snapshotted_so_later_mutation_cannot_corrupt_replan():
    bal = {"USD": D("3000"), "EUR": D("1500"), "GBP": D("0")}
    src = demo_inputs()
    inp = replace(src, balances=bal)
    prev = make_plan(inp)
    bal["EUR"] = D("0")  # caller mutates its own dict afterwards
    assert inp.balances["EUR"] == D("1500")
    assert isinstance(inp.balances, MappingProxyType)
    with pytest.raises(TypeError):
        inp.balances["EUR"] = D("0")
    new = replace(inp, balances={**inp.balances, "EUR": D("0")})
    key = lambda p: [(d.obligation_id, d.action, d.reserves) for d in p.decisions]
    assert key(replan(prev, new).plan) == key(make_plan(new))
    with pytest.raises(TypeError):
        inp.rates.sell_per_buy[("USD", "EUR")] = D("9")


def test_f9_duplicate_obligation_or_receipt_ids_are_rejected():
    inp = demo_inputs()
    dup = Obligation("O1", "Other", "b", D("1"), "EUR", h(5))
    with pytest.raises(ValueError):
        replace(inp, obligations=inp.obligations + (dup,))
    with pytest.raises(ValueError):
        replace(inp, receipts=inp.receipts + (inp.receipts[0],))


def test_f10_missing_rate_does_not_block_a_receipt_funded_deferral():
    inp = demo_inputs()
    ob = Obligation("J", "J", "b", D("100"), "JPY", h(48))
    r = Receipt("RJ", "P", D("100"), "JPY", h(20), Confidence.HIGH)
    plan = make_plan(replace(inp, obligations=(ob,), receipts=(r,), balances={"USD": D("0")}))
    assert plan.decision("J").action.value == "DEFER"
    plan2 = make_plan(replace(inp, obligations=(ob,), receipts=(), balances={"USD": D("0")}))
    assert plan2.decision("J").reason_code == "NO_RATE"


# ---- F14: exact decimals --------------------------------------------------------------------------------

def test_f14_decimals_round_trip_exactly():
    for s in ("90071992547409.91", "1.30", "0.000000123456789", "1306.50"):
        out = wire.loads(wire.dumps({"a": D(s)}))["a"]
        assert out == D(s) and str(out) == s.rstrip("0") or out == D(s)
    assert wire.loads(wire.dumps({"a": D("90071992547409.91")}))["a"] == D("90071992547409.91")
    with pytest.raises(ValueError):
        wire.dumps({"a": D("NaN")})


# ---- F15: lookups fail closed on pagination ---------------------------------------------------------------

def test_f15_a_paged_lookup_result_fails_closed():
    a = HttpAdapter("http://127.0.0.1:1", "id", "key")
    a._call = lambda *args, **kw: {"items": [], "has_more": True}
    with pytest.raises(AirwallexError) as e:
        a.find_conversions("r")
    assert e.value.code == "incomplete_lookup"
    with pytest.raises(AirwallexError):
        a.find_transfers("r")


# ---- F16: transport behaviour that was untested ---------------------------------------------------------------

def test_f16_get_401_refreshes_the_token_but_a_post_401_is_never_resent():
    from rate_locked.awx.fake import FakeAirwallex
    from rate_locked.awx.fake_server import FakeServer
    fake = FakeAirwallex.demo()
    with FakeServer(fake) as server:
        a = HttpAdapter(server.base_url, "id", "key")  # wall-clock token bookkeeping: server expires it first
        a.get_balances()
        fake.advance(31 * 60)
        a.get_balances()
        assert sum(1 for r in server.requests if r[1].endswith("/authentication/login")) == 2
        q = a.create_quote(buy_currency="GBP", sell_currency="USD", validity="MIN_1", buy_amount=D("10"))
        fake.advance(31 * 60)
        before = sum(1 for r in server.requests if r == ("POST", "/api/v1/fx/conversions/create"))
        with pytest.raises(AirwallexError):
            a.create_conversion(request_id="r401", quote_id=q.quote_id, buy_currency="GBP", sell_currency="USD",
                                buy_amount=D("10"))
        after = sum(1 for r in server.requests if r == ("POST", "/api/v1/fx/conversions/create"))
        assert after - before == 1

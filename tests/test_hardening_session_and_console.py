"""Regression tests for the session and console hardening cases (N1 to N11)."""
import http.client
import io
import json
import threading
import urllib.error
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D

import pytest

from rate_locked.approval import ApprovalError
from rate_locked.awx import wire
from rate_locked.awx.adapter import AirwallexError, UncertainOutcome
from rate_locked.awx.http_adapter import HttpAdapter, check_host
from rate_locked.calllog import CallLog
from rate_locked.executor import EventLog, State, StateError
from rate_locked.record import verify_record

from helpers import World
from test_probe import CID, KEY, probe, _scripted_http


# ---- N1: every later step runs from the gate's bound envelope ----------------------------------------

def test_n1_payout_never_uses_a_tampered_envelope_or_approval():
    w = World(settle_after_polls=2)
    ex = w.executor.dispatch(w.approved("O2").execution_id)
    assert ex.state is State.CONVERTING
    ex._envelope = replace(ex._envelope, payout_amount=D("5"), beneficiary_id="other")
    ex.approval = None
    for _ in range(4):
        w.executor.advance(ex.execution_id)
    for t in w.fake._transfers.values():
        assert t.transfer_amount == D("1000.00") and t.beneficiary_id == w.ben.beneficiary_id
    assert any(e.kind == "envelope_mismatch_halted" for e in ex.events)


def test_n1_record_is_built_from_the_bound_envelope():
    w = World()
    ex = w.executor.dispatch(w.approved("O1").execution_id)
    ex._envelope = replace(ex._envelope, payout_amount=D("1"))
    rec = w.executor.record(ex.execution_id).to_dict()
    assert rec["envelope"]["payout_amount"] == "1200.00"
    assert rec["all_match"] is True and verify_record(rec, expected_envelope_digest=w.gate.bound_envelope(ex.execution_id).digest) == []


def test_n1_reconcile_also_runs_from_the_bound_envelope():
    w = World()
    ex = w.approved("O2")
    w.fake.inject("create_conversion", "drop_response")
    ex = w.executor.dispatch(ex.execution_id)
    ex._envelope = replace(ex._envelope, payout_amount=D("5"))
    ex = w.executor.reconcile(ex.execution_id)
    assert ex.state is State.UNKNOWN  # halted: the object no longer matches the approval
    assert w.executor.call_counts(ex.execution_id)["transfer_create"] == 0


# ---- N2: one live execution per obligation ------------------------------------------------------------

def lapse(w, oid="O2"):
    prep = w.prepare(oid)
    w.clock.advance(61)
    w.executor.approve(prep.token, prep.execution.envelope.digest, "a")
    return prep.execution


def test_n2_double_requote_cannot_create_two_payable_approvals():
    w = World()
    old = lapse(w)
    w.executor.requote(old.execution_id)
    with pytest.raises(StateError):
        w.executor.requote(old.execution_id)


def test_n2_second_prepare_for_the_same_obligation_is_refused_while_one_is_live():
    w = World()
    w.prepare("O1")
    with pytest.raises(StateError):
        w.prepare("O1")


def test_n2_paid_obligation_cannot_be_prepared_again():
    w = World()
    ex = w.executor.dispatch(w.approved("O1").execution_id)
    assert ex.state is State.PAYOUT_REPORTED
    with pytest.raises(StateError):
        w.prepare("O1")


def test_n2_denied_or_expired_obligation_can_be_asked_again():
    w = World()
    prep = w.prepare("O1")
    w.executor.deny(prep.token, "a")
    assert w.prepare("O1").token


# ---- N3: reconcile adopts only a record that belongs to this request ---------------------------------------

def test_n3_foreign_record_in_a_lookup_is_not_adopted():
    w = World()
    ex = w.approved("O2")
    w.fake.inject("create_conversion", "drop_request")
    ex = w.executor.dispatch(ex.execution_id)
    real_find = w.fake.find_conversions
    foreign = replace(w.fake.create_conversion(
        request_id="someone-else", quote_id=w.fake.create_quote(buy_currency="GBP", sell_currency="USD",
                                                                validity="MIN_15", buy_amount=D("10")).quote_id,
        buy_currency="GBP", sell_currency="USD", buy_amount=D("10")))
    w.fake.find_conversions = lambda request_id: [foreign]
    ex = w.executor.reconcile(ex.execution_id)
    assert ex.state is State.INDETERMINATE
    assert w.executor.call_counts(ex.execution_id)["transfer_create"] == 0
    w.fake.find_conversions = real_find


# ---- N4: probe handles unreadable error bodies and non-object success bodies --------------------------------

class _BadFp:
    def read(self, *a):
        raise http.client.IncompleteRead(b"")

    def close(self):
        pass


class _Raises:
    def open(self, req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 502, "bad gateway", {}, _BadFp())


def test_n4_unreadable_error_body_does_not_crash_the_transport(monkeypatch):
    monkeypatch.setattr(probe, "_opener", _Raises())
    status, data, _h = probe.http("GET", "http://127.0.0.1:9/x", headers={})
    assert status == 502 and isinstance(data, dict)


def _convert_script(conv_response):
    def script(method, url, headers, body):
        if url.endswith("/authentication/login"):
            return 201, {"token": "t-123456789", "expires_at": "x"}, {}
        if url.endswith("/fx/quotes/create"):
            return 201, {"quote_id": "q1", "client_rate": "1.3"}, {}
        if url.endswith("/fx/conversions/create"):
            return conv_response
        return 200, {}, {}
    return script


@pytest.mark.parametrize("response", [(502, {"_non_json": True}, {}), (201, [], {}), (200, {}, {}), (0, {}, {})])
def test_n4_unreadable_or_non_object_conversion_answers_are_looked_up_and_unresolved(monkeypatch, tmp_path, capsys, response):
    monkeypatch.setenv("AIRWALLEX_CLIENT_ID", CID)
    monkeypatch.setenv("AIRWALLEX_API_KEY", KEY)
    seen = []
    base = _convert_script(response)

    def script(method, url, headers, body):
        if "/fx/conversions?" in url:
            seen.append(url)
        return base(method, url, headers, body)

    monkeypatch.setattr(probe, "http", _scripted_http(script))
    code = probe.main(["--base-url", "http://127.0.0.1:9", "--out", str(tmp_path), "--convert"], sleep=lambda s: None)
    out = capsys.readouterr().out
    assert code == 3 and len(seen) == 4 and "UNRESOLVED" in out


# ---- N5: a voided execution leaves no spendable approval at the gate ------------------------------------------

def test_n5_preparation_failure_spends_the_gate_approval():
    w = World()
    ex = w.approved("O2")
    w.fake.get_quote = lambda qid: (_ for _ in ()).throw(AirwallexError("not_found", "x", 404))
    ex = w.executor.dispatch(ex.execution_id)
    assert ex.state is State.VOIDED
    with pytest.raises(ApprovalError):
        w.gate.authorize(ex.execution_id, ex.envelope.quote, ex.envelope.digest)


# ---- N6: non-JSON bodies are never stored in evidence ------------------------------------------------------------

def test_n6_non_json_login_body_leaves_no_secret_fragment(monkeypatch, tmp_path):
    monkeypatch.setenv("AIRWALLEX_CLIENT_ID", CID)
    monkeypatch.setenv("AIRWALLEX_API_KEY", KEY)

    class Resp:
        status = 200
        headers = {}

        def read(self):
            return (b"x" * 280 + KEY.encode() + b" token=TOKSECRET-987654").decode().encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class Op:
        def open(self, req, timeout=None):
            return Resp()

    monkeypatch.setattr(probe, "_opener", Op())
    probe.main(["--base-url", "http://127.0.0.1:9", "--out", str(tmp_path)])
    text = next(tmp_path.glob("probe_*.json")).read_text()
    assert KEY[:10] not in text and "TOKSECRET" not in text


# ---- N7, N8, N9: adapter hygiene -----------------------------------------------------------------------------------

class _Op400:
    def open(self, req, timeout=None):
        if req.full_url.endswith("/authentication/login"):
            class R:
                status = 201

                def read(self):
                    return b'{"token": "t", "expires_at": "2999-01-01T00:00:00+0000"}'

                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False
            return R()
        raise urllib.error.HTTPError(req.full_url, 400, "bad", {}, io.BytesIO(b'["x"]'))


def test_n7_non_object_error_body_on_a_create_is_uncertain_not_a_failure():
    """Changed on purpose (F2): a 400 whose body cannot be classified may be a live duplicate."""
    a = HttpAdapter("http://127.0.0.1:1", "id", "key")
    a._opener = _Op400()
    with pytest.raises(UncertainOutcome):
        a.create_transfer(request_id="r", beneficiary_id="b", source_currency="EUR", transfer_currency="EUR",
                          transfer_amount=D("1"), reason="r", reference="x")


@pytest.mark.parametrize("bad", ["http://user:pw@127.0.0.1:1", "http://evil.invalid\\@127.0.0.1:1",
                                 "http://127.0.0.1:1/path", "http://127.0.0.1:1?x=1", "http://127.0.0.1:1#f"])
def test_n8_http_adapter_host_lock_is_as_strict_as_the_probe(bad):
    with pytest.raises(ValueError):
        check_host(bad)
    with pytest.raises(probe.HostError):
        probe.check_base(bad)


def test_n9_non_finite_numbers_are_rejected_at_the_boundary():
    with pytest.raises(ValueError):
        wire.loads('{"a": NaN}')
    with pytest.raises(ValueError):
        wire.dec("NaN")
    with pytest.raises(ValueError):
        wire.dec("Infinity")


def test_n9_adapter_turns_a_nan_balance_into_a_provider_error():
    class Op:
        def open(self, req, timeout=None):
            class R:
                status = 200

                def read(self_inner):
                    if req.full_url.endswith("/authentication/login"):
                        return b'{"token": "t", "expires_at": "2999-01-01T00:00:00+0000"}'
                    return b'[{"currency": "USD", "available_amount": NaN}]'

                def __enter__(self_inner):
                    return self_inner

                def __exit__(self_inner, *a):
                    return False
            return R()

    a = HttpAdapter("http://127.0.0.1:1", "id", "key")
    a._opener = Op()
    with pytest.raises(AirwallexError):
        a.get_balances()


# ---- N10: logs are thread-safe across executions -----------------------------------------------------------------------

class _Dummy:
    def get_balances(self):
        return []


def test_n10_call_log_and_event_log_survive_many_threads():
    log = CallLog(_Dummy())
    ev = EventLog()
    from rate_locked.clock import ManualClock
    now = ManualClock().now()

    def work(i):
        for j in range(200):
            log.get_balances()
            ev.append(f"e{i}", now, "k", {})

    threads = [threading.Thread(target=work, args=(i,)) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(log.log) == 1600 and all(c.outcome == "ok" for c in log.log)
    assert len({c.seq for c in log.log}) == 1600
    assert len({e.seq for e in ev.events}) == 1600


# ---- N11: only economically relevant quote fields are compared -----------------------------------------------------

def test_n11_live_quote_with_formatting_differences_is_accepted_but_a_changed_rate_is_not():
    w = World()
    ex = w.approved("O2")
    q = ex.envelope.quote
    live_same = replace(q, usage="", currency_pair="USDGBP")
    w.gate.authorize(ex.execution_id, live_same, ex.envelope.digest)  # no error
    w2 = World()
    ex2 = w2.approved("O2")
    with pytest.raises(ApprovalError) as e:
        w2.gate.authorize(ex2.execution_id, replace(ex2.envelope.quote, client_rate=D("1.31")), ex2.envelope.digest)
    assert e.value.code == "TERMS_CHANGED"

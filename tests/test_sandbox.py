"""The sandbox world for the console, tested without the live sandbox.

Nothing here touches api.sandbox.airwallex.com: the HTTP adapter is pointed at the local fake server on loopback, and
the label stays FAKE there (the world label is SANDBOX only when the adapter really points at the sandbox host).
"""
import json

import pytest

from rate_locked.awx.fake import FakeAirwallex
from rate_locked.awx.fake_server import FakeServer
from rate_locked.clock import ManualClock
from rate_locked.sandbox import (ENV_BENEFICIARIES, ENV_ID, ENV_KEY, SandboxSetupError, build_sandbox_session,
                                 load_beneficiaries, supplier_names)
from rate_locked.scenario import NOW
from rate_locked.session import DIGEST_PREFIX, Session

from helpers import resolve_review

GOOD = {"bank_details": {"account_currency": "EUR", "account_name": "Test", "bank_country_code": "DE",
                         "iban": "DE00000000000000000000"}, "entity_type": "COMPANY", "company_name": "Test"}


def write_bens(tmp_path, **over):
    data = {n: dict(GOOD) for n in supplier_names()}
    data.update(over)
    p = tmp_path / "bens.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return str(p)


def approve(s, token):
    view = s.approver_view(token)
    return s.approver_approve(token, view["digest"], view["digest"][:DIGEST_PREFIX])


# ---- refusals: the owner has to supply keys and real beneficiary details -------------------------------------------

def test_refuses_without_both_keys_and_says_how_to_set_them_safely():
    with pytest.raises(SandboxSetupError) as e:
        build_sandbox_session(env={ENV_ID: "only-the-id"})
    assert ENV_KEY in str(e.value) and "Read-Host" in str(e.value) and "only-the-id" not in str(e.value)


def test_refuses_without_a_beneficiary_file(tmp_path):
    with pytest.raises(SandboxSetupError, match="probe"):
        build_sandbox_session(env={ENV_ID: "i", ENV_KEY: "k"})
    with pytest.raises(SandboxSetupError, match="Cannot read"):
        load_beneficiaries(str(tmp_path / "missing.json"))


def test_refuses_placeholders_and_missing_suppliers(tmp_path):
    bad = write_bens(tmp_path, **{supplier_names()[0]: {"bank_details": {"iban": "REPLACE_WITH_SANDBOX_IBAN"}}})
    with pytest.raises(SandboxSetupError, match="placeholder"):
        load_beneficiaries(bad)
    only_one = tmp_path / "one.json"
    only_one.write_text(json.dumps({supplier_names()[0]: GOOD}), encoding="utf-8")
    with pytest.raises(SandboxSetupError, match="no entry for"):
        load_beneficiaries(str(only_one))
    nobank = tmp_path / "nobank.json"
    nobank.write_text(json.dumps({n: {"entity_type": "COMPANY"} for n in supplier_names()}), encoding="utf-8")
    with pytest.raises(SandboxSetupError, match="bank_details"):
        load_beneficiaries(str(nobank))


def test_example_file_in_docs_is_all_placeholders_so_it_cannot_be_used_as_is():
    from pathlib import Path
    example = Path(__file__).resolve().parents[1] / "docs" / "sandbox_beneficiaries.example.json"
    with pytest.raises(SandboxSetupError, match="placeholder"):
        load_beneficiaries(str(example))


def test_host_lock_refuses_any_other_host(tmp_path):
    with pytest.raises(SandboxSetupError, match="host lock"):
        build_sandbox_session(env={ENV_ID: "i", ENV_KEY: "k"}, base_url="https://example.com",
                              beneficiaries_path=write_bens(tmp_path))


def test_preflight_failure_is_reported_without_the_key(tmp_path):
    fake = FakeAirwallex.demo(clock=ManualClock(NOW))
    server = FakeServer(fake, credentials=("good-id", "good-key")).start()
    try:
        with pytest.raises(SandboxSetupError) as e:
            build_sandbox_session(env={ENV_ID: "bad-id", ENV_KEY: "bad-secret-key"}, base_url=server.base_url,
                                  clock=fake.clock, beneficiaries_path=write_bens(tmp_path))
        assert "credentials_invalid" in str(e.value) and "bad-secret-key" not in str(e.value)
    finally:
        server.close()


# ---- the whole money path through the HTTP adapter and the console session --------------------------------------------

@pytest.fixture
def served(tmp_path):
    clock = ManualClock(NOW)
    fake = FakeAirwallex.demo(clock=clock)
    server = FakeServer(fake, credentials=("good-id", "good-key")).start()
    session, pre = build_sandbox_session(env={ENV_ID: "good-id", ENV_KEY: "good-key"}, base_url=server.base_url,
                                         clock=clock, run_id="r-sbx", commit="abc1234",
                                         beneficiaries_path=write_bens(tmp_path))
    yield session, pre, fake, clock
    server.close()


def test_world_label_is_fake_unless_the_adapter_points_at_the_sandbox_host(served):
    s, pre, _fake, _clock = served
    assert pre["world"] == "FAKE" and s.world == "FAKE"
    assert s.state()["chips"]["world"] == "FAKE" and pre["base"].startswith("http://127.0.0.1:")
    assert s.state()["controls"] is False  # no test controls without the fake's own handle


def test_planning_time_is_pinned_and_beneficiary_details_reach_the_provider(served):
    s, _pre, fake, clock = served
    resolve_review(s)
    clock.advance(3 * 24 * 3600)  # real time moves on; the fixture's planning time does not
    st = s.state()
    assert st["policy"]["planning_pinned"] is True and st["policy"]["planning_now"].startswith("2026-11-02T09:00")
    assert [o["obligation_id"] for o in st["obligations"]] == ["O1", "O2", "O3"]
    assert all(c["op"] != "create_conversion" for c in st["calls"])
    assert len(fake._beneficiaries) == 3  # all three created through POST /beneficiaries/create


def test_convert_then_pay_through_http_adapter_with_the_session(served):
    s, _pre, fake, _clock = served
    resolve_review(s)
    prep = s.prepare("O2")
    assert approve(s, prep["token"])["ok"] is True
    out = s.dispatch(prep["execution_id"])
    assert out["state"] in ("PAYOUT_REPORTED", "PAID")
    assert out["counts"]["conversion_create"] == 1 and out["counts"]["transfer_create"] == 1
    assert out["conversion"]["quote_id"] == out["quote"]["quote_id"]


# ---- the builder guide's "simulate SENT before PAID" is applied in the SANDBOX world only ---------------------------------

class _Recorder(FakeAirwallex):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.transitions = []

    def simulate_transfer_transition(self, transfer_id, transition):
        self.transitions.append(transition)
        if transition == "SENT":
            return self.get_transfer(transfer_id)
        return super().simulate_transfer_transition(transfer_id, transition)


def _run_paid(world):
    clock = ManualClock(NOW)
    fake = _Recorder(balances={"USD": "3000.00", "EUR": "1500.00", "GBP": "0.00"},
                     pairs={"EURUSD": "1.08", "GBPUSD": "1.30"}, clock=clock)
    s = Session(fake, world=world, clock=clock, run_id="r-t", commit="abc", plan_now=NOW)
    resolve_review(s)
    prep = s.prepare("O1")
    assert approve(s, prep["token"])["ok"]
    s.dispatch(prep["execution_id"])
    s.simulate_transition(prep["execution_id"], "PAID")
    return fake.transitions


def test_sent_is_simulated_before_paid_in_the_sandbox_world_only():
    assert _run_paid("SANDBOX") == ["SENT", "PAID"]
    assert _run_paid("FAKE") == ["PAID"]


# ---- the real sandbox wallet is huge, so the agent works from a stated float ------------------------------------------

def test_the_agent_plans_from_a_float_and_tracks_the_real_wallet_movement(tmp_path):
    """Live sandbox wallets hold 10,000,000 of USD, EUR and GBP, so the story (USD 3,000, EUR 1,500, GBP 0) would
    never need a conversion. The sandbox session shows the float plus what the wallet moved since start."""
    clock = ManualClock(NOW)
    big = {"USD": "10000000", "EUR": "10000000", "GBP": "10000000"}
    fake = FakeAirwallex(balances=big, pairs={"EURUSD": "1.08", "GBPUSD": "1.30"}, clock=clock)
    server = FakeServer(fake, credentials=("good-id", "good-key")).start()
    try:
        s, pre = build_sandbox_session(env={ENV_ID: "good-id", ENV_KEY: "good-key"}, base_url=server.base_url,
                                       clock=clock, beneficiaries_path=write_bens(tmp_path))
        resolve_review(s)
        shown = lambda: {r["currency"]: r["available"] for r in s.state()["balances"]["rows"]}  # noqa: E731
        assert shown() == {"USD": "3,000.00", "EUR": "1,500.00", "GBP": "0.00"}
        assert "demo allocation" in s.state()["balances"]["note"].lower()
        prep = s.prepare("O2")
        assert approve(s, prep["token"])["ok"] is True
        s.dispatch(prep["execution_id"])
        after = shown()
        assert after["GBP"] == "0.00" and after["USD"] != "3,000.00"  # 1,000 GBP bought and paid out; USD spent
    finally:
        server.close()


# ---- the stated float can never exceed what the wallet really holds ---------------------------------------------------

def test_refuses_to_start_when_the_wallet_cannot_cover_the_float(tmp_path):
    clock = ManualClock(NOW)
    small = {"USD": "2999.99", "EUR": "10000000", "GBP": "0"}
    fake = FakeAirwallex(balances=small, pairs={"EURUSD": "1.08", "GBPUSD": "1.30"}, clock=clock)
    server = FakeServer(fake, credentials=("good-id", "good-key")).start()
    try:
        with pytest.raises(SandboxSetupError, match="USD"):
            build_sandbox_session(env={ENV_ID: "good-id", ENV_KEY: "good-key"}, base_url=server.base_url,
                                  clock=clock, beneficiaries_path=write_bens(tmp_path))
    finally:
        server.close()


def test_float_adapter_never_shows_more_than_the_wallet_holds_now():
    from decimal import Decimal as D
    from rate_locked.awx.adapter import Balance
    from rate_locked.sandbox import FloatAdapter

    class Inner:
        def __init__(self):
            self.now = {"USD": D("3000"), "EUR": D("1500"), "GBP": D("0")}

        def get_balances(self):
            return [Balance(c, a) for c, a in self.now.items()]

    inner = Inner()
    fa = FloatAdapter(inner, {"USD": D("3000"), "EUR": D("1500"), "GBP": D("0")})
    inner.now["USD"] = D("2000")  # money left the wallet by some other route
    assert {b.currency: b.available for b in fa.get_balances()}["USD"] == D("2000")
    inner.now["USD"] = D("4000")  # money arrived: the float grows with the movement
    assert {b.currency: b.available for b in fa.get_balances()}["USD"] == D("4000")
    big = FloatAdapter(inner, {"USD": D("100"), "EUR": D("1500"), "GBP": D("0")})  # wallet held less than the float at start
    assert {b.currency: b.available for b in big.get_balances()}["USD"] <= D("4000")
    assert "demo allocation" in FloatAdapter.float_note.lower() and "fresh allocation" in FloatAdapter.float_note.lower()

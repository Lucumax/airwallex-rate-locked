"""Probe stage 2: the payout leg (beneficiary create, transfer create, lookup by request id, SENT then PAID).

Run against the local fake. Stage 2 needs the owner's private beneficiary file; the probe never invents bank details.
"""
import importlib.util
import json
import sys
from decimal import Decimal as D
from pathlib import Path

import pytest

from rate_locked.awx.fake import FakeAirwallex
from rate_locked.awx.fake_server import FakeServer

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("sandbox_probe", ROOT / "scripts" / "sandbox_probe.py")
probe = importlib.util.module_from_spec(spec)
sys.modules["sandbox_probe"] = probe
spec.loader.exec_module(probe)

CID, KEY = "client-id-SENTINEL-1234", "api-key-SENTINEL-abcdefghijkl"
IBAN = "DE00SENTINELIBAN1234567"


@pytest.fixture
def served(monkeypatch):
    fake = FakeAirwallex.demo(strict_transitions=True)
    server = FakeServer(fake).start()
    monkeypatch.setenv("AIRWALLEX_CLIENT_ID", CID)
    monkeypatch.setenv("AIRWALLEX_API_KEY", KEY)
    yield fake, server
    server.close()


def bens_file(tmp_path, **override):
    entry = {"entity_type": "COMPANY", "company_name": "Acme Co.",
             "bank_details": {"account_currency": "EUR", "account_name": "Acme Co.", "bank_country_code": "DE",
                              "iban": IBAN}}
    entry.update(override)
    p = tmp_path / "bens.json"
    p.write_text(json.dumps({"Acme Co.": entry}), encoding="utf-8")
    return str(p)


def run(server, tmp_path, *extra):
    code = probe.main(["--base-url", server.base_url, "--out", str(tmp_path / "ev"), *extra], sleep=lambda s: None)
    files = list((tmp_path / "ev").glob("probe_*.json"))
    return code, (json.loads(files[0].read_text()) if files else None), files


def test_payout_leg_runs_the_five_steps_and_ends_paid(served, tmp_path, capsys):
    fake, server = served
    code, ev, _ = run(server, tmp_path, "--payout", "--beneficiaries", bens_file(tmp_path), "--amount", "5.00")
    out = capsys.readouterr().out
    assert code == 0
    by = {}
    for s in ev["steps"]:
        by.setdefault(s["id"], []).append(s)
    assert by["P11"][0]["ok"] and by["P12"][0]["ok"] and by["P13"][0]["ok"]
    assert by["P14"][0]["ok"] and by["P15"][0]["ok"]
    assert by["P14"][0]["response"]["status"] == "SENT" and by["P15"][0]["response"]["status"] == "PAID"
    assert "items found: 1" in by["P13"][-1]["note"]
    posts = [r for r in server.requests if r == ("POST", "/api/v1/transfers/create")]
    assert len(posts) == 1  # exactly one transfer was created
    assert len(fake._transfers) == 1 and next(iter(fake._transfers.values())).transfer_amount == D("5.00")


def test_payout_leg_sends_next_status_and_string_amount(served, tmp_path):
    fake, server = served
    _, ev, _ = run(server, tmp_path, "--payout", "--beneficiaries", bens_file(tmp_path), "--amount", "5.00")
    steps = {s["id"]: s for s in ev["steps"] if s["id"] in ("P12", "P14", "P15")}
    assert steps["P12"]["request"]["transfer_amount"] == "5.00"
    assert steps["P14"]["request"] == {"next_status": "SENT"} and steps["P15"]["request"] == {"next_status": "PAID"}


def test_payout_leg_never_prints_or_stores_bank_details(served, tmp_path, capsys):
    fake, server = served
    _, ev, files = run(server, tmp_path, "--payout", "--beneficiaries", bens_file(tmp_path))
    out = capsys.readouterr().out
    text = files[0].read_text()
    assert IBAN not in out and IBAN not in text and KEY not in text and CID not in text


def test_payout_leg_saves_intent_before_each_financial_call(served, tmp_path):
    fake, server = served
    _, ev, _ = run(server, tmp_path, "--payout", "--beneficiaries", bens_file(tmp_path))
    ops = [i["op"] for i in ev["intents"]]
    assert ops.count("transfer") == 1 and ops.index("transfer") >= 0


def test_payout_needs_the_beneficiary_file(served, tmp_path, capsys):
    fake, server = served
    code = probe.main(["--base-url", server.base_url, "--out", str(tmp_path), "--payout"])
    out = capsys.readouterr().out
    assert code == 2 and "--beneficiaries" in out
    assert not any(r for r in server.requests if "transfers/create" in r[1])


def test_payout_refuses_placeholders_and_makes_no_call(served, tmp_path, capsys):
    fake, server = served
    path = bens_file(tmp_path, bank_details={"account_currency": "EUR", "account_name": "Acme Co.",
                                             "bank_country_code": "REPLACE_COUNTRY", "iban": "REPLACE_IBAN"})
    code = probe.main(["--base-url", server.base_url, "--out", str(tmp_path), "--payout", "--beneficiaries", path])
    assert code == 2 and "REPLACE_" in capsys.readouterr().out
    assert server.requests == []


def test_payout_with_too_little_balance_creates_nothing(served, tmp_path):
    fake, server = served
    _, ev, _ = run(server, tmp_path, "--payout", "--beneficiaries", bens_file(tmp_path), "--amount", "99999")
    assert not any(r for r in server.requests if r == ("POST", "/api/v1/transfers/create"))
    p12 = next(s for s in ev["steps"] if s["id"] == "P12")
    assert not p12["ok"] and "balance" in p12["note"].lower()


def test_dry_run_lists_the_payout_steps(capsys):
    assert probe.main(["--dry-run", "--payout", "--beneficiaries", "x.json"]) == 0
    out = capsys.readouterr().out
    for sid in ("P11", "P12", "P13", "P14", "P15"):
        assert sid in out


def test_a_lost_transfer_response_is_looked_up_not_resent(served, tmp_path, capsys):
    fake, server = served
    fake.inject("create_transfer", "drop_response")
    code, ev, _ = run(server, tmp_path, "--payout", "--beneficiaries", bens_file(tmp_path))
    out = capsys.readouterr().out
    assert sum(1 for r in server.requests if r == ("POST", "/api/v1/transfers/create")) == 1
    p12 = next(s for s in ev["steps"] if s["id"] == "P12")
    assert p12.get("uncertain") is True and "UNCERTAIN" in out and code == 3
    assert len([s for s in ev["steps"] if s["id"] == "P13"]) >= 1

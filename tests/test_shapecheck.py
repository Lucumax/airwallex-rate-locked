"""check-shapes: compare a probe evidence file with the parsers in wire.py, step by step."""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from rate_locked.awx.fake import FakeAirwallex
from rate_locked.awx.fake_server import FakeServer
from rate_locked.shapecheck import check_evidence, render

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("sandbox_probe", ROOT / "scripts" / "sandbox_probe.py")
probe = importlib.util.module_from_spec(spec)
sys.modules["sandbox_probe"] = probe
spec.loader.exec_module(probe)


@pytest.fixture
def evidence(monkeypatch, tmp_path):
    fake = FakeAirwallex.demo(strict_transitions=True)
    server = FakeServer(fake).start()
    monkeypatch.setenv("AIRWALLEX_CLIENT_ID", "cid-for-tests")
    monkeypatch.setenv("AIRWALLEX_API_KEY", "key-for-tests-123456")
    bens = tmp_path / "b.json"
    bens.write_text(json.dumps({"Acme Co.": {"company_name": "Acme Co.", "bank_details": {
        "account_currency": "EUR", "account_name": "Acme Co.", "bank_country_code": "DE", "iban": "DE00TEST"}}}))
    try:
        probe.main(["--base-url", server.base_url, "--out", str(tmp_path / "ev"), "--convert", "--simulate-deposit",
                    "--payout", "--beneficiaries", str(bens)], sleep=lambda s: fake.advance(s))
    finally:
        server.close()
    return json.loads(next((tmp_path / "ev").glob("probe_*.json")).read_text())


def rows_by_step(rows):
    return {r.step: r for r in rows}


def test_a_full_probe_against_the_documented_shapes_has_no_mismatch(evidence):
    rows = check_evidence(evidence)
    assert not [r for r in rows if r.status == "MISMATCH"], render(rows)
    by = rows_by_step(rows)
    for sid in ("P2", "P4b", "P5", "P6", "P7", "P11", "P12", "P13", "P14", "P15", "SIM0", "SIM"):
        assert by[sid].status == "OK", (sid, by.get(sid))


def test_a_renamed_field_is_named_in_the_mismatch(evidence):
    for s in evidence["steps"]:
        if s["id"] == "P11":
            s["response"]["beneficiary_id"] = s["response"].pop("id")  # the old guess
    row = rows_by_step(check_evidence(evidence))["P11"]
    assert row.status == "MISMATCH" and "id" in row.detail


def test_steps_that_did_not_run_are_skipped_not_failed(evidence):
    evidence["steps"] = [s for s in evidence["steps"] if s["id"] in ("P1", "P2")]
    rows = check_evidence(evidence)
    assert [r.step for r in rows] == ["P2"]


def test_a_failed_step_is_reported_as_not_checked(evidence):
    for s in evidence["steps"]:
        if s["id"] == "P5":
            s["ok"], s["http_status"], s["response"] = False, 403, {"code": "forbidden"}
    row = rows_by_step(check_evidence(evidence))["P5"]
    assert row.status == "NOT_CHECKED" and "403" in row.detail


def test_render_is_a_table_with_a_verdict(evidence):
    text = render(check_evidence(evidence))
    assert "P11" in text and "OK" in text and "Verdict" in text


def test_cli_exit_codes(evidence, tmp_path, capsys):
    from rate_locked.__main__ import main
    good = tmp_path / "good.json"
    good.write_text(json.dumps(evidence))
    assert main(["check-shapes", str(good)]) == 0
    for s in evidence["steps"]:
        if s["id"] == "P2":
            s["response"] = [{"currency": "USD"}]  # no available_amount
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(evidence))
    assert main(["check-shapes", str(bad)]) == 1
    assert "MISMATCH on P2" in capsys.readouterr().out
    assert main(["check-shapes", str(tmp_path / "missing.json")]) == 2


def test_truncation_marker_in_a_recorded_list_is_not_a_mismatch():
    """The real sandbox returns ~47 balance lines; the probe keeps 20 and appends a marker string. The marker is
    the sanitizer's, not the provider's, so the balances parser must still be judged on the real lines."""
    line = {"account_type": "cash", "available_amount": 0, "currency": "AED", "pending_amount": 0,
            "prepayment_amount": 0, "reserved_amount": 0, "total_amount": 0}
    ev = {"steps": [{"id": "P2", "ok": True, "http_status": 200, "response": [line, "[26 more items not shown]"]}]}
    assert check_evidence(ev)[0].status == "OK"

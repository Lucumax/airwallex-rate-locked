import copy
import json
from dataclasses import replace
from decimal import Decimal as D

from rate_locked.record import build_record, verify_record

from helpers import World


def run_convert(w):
    ex = w.approved("O2")
    return w.executor.dispatch(ex.execution_id)


def row(rec, name):
    return next(r for r in rec.rows if r.field == name)


def test_record_pairs_approved_and_executed_for_a_completed_conversion():
    w = World()
    ex = run_convert(w)
    rec = build_record(ex, w.executor.call_counts(ex.execution_id), w.clock.now())
    assert rec.complete and rec.all_match
    assert row(rec, "rate").approved == row(rec, "rate").executed == "1.30"
    assert row(rec, "quote_id").matches is True
    assert row(rec, "conversion_status").executed == "SETTLED" and row(rec, "conversion_status").matches is None
    assert row(rec, "payout_status").executed == "PROCESSING"  # as reported, never PAID early
    assert rec.call_counts == {"conversion_create": 1, "transfer_create": 1}


def test_fund_record_has_no_conversion_rows():
    w = World()
    ex = w.approved("O1")
    ex = w.executor.dispatch(ex.execution_id)
    rec = build_record(ex, w.executor.call_counts(ex.execution_id), w.clock.now())
    assert not any(r.field.startswith("conversion") or r.field in ("rate", "quote_id", "pair") for r in rec.rows)
    assert rec.complete and rec.all_match


def test_executed_terms_that_differ_are_flagged():
    w = World()
    ex = run_convert(w)
    ex.conversion = replace(ex.conversion, client_rate=D("1.31"), sell_amount=D("1310.00"))
    rec = build_record(ex, {"conversion_create": 1, "transfer_create": 1}, w.clock.now())
    assert row(rec, "rate").matches is False and row(rec, "sell_amount").matches is False
    assert not rec.all_match


def test_record_before_execution_is_incomplete_not_matching():
    w = World()
    ex = w.approved("O2")
    rec = build_record(ex, w.executor.call_counts(ex.execution_id), w.clock.now())
    assert not rec.complete and not rec.all_match
    assert row(rec, "rate").executed == "-" and row(rec, "rate").matches is None


def test_bundle_has_no_secret_or_token_material():
    w = World()
    prep = w.prepare("O2")
    w.executor.approve(prep.token, prep.execution.envelope.digest, "a")
    ex = w.executor.dispatch(prep.execution.execution_id)
    blob = json.dumps(build_record(ex, {}, w.clock.now()).to_dict())
    assert prep.token not in blob


def bundle(w):
    ex = run_convert(w)
    return build_record(ex, w.executor.call_counts(ex.execution_id), w.clock.now()).to_dict()


def test_verify_passes_on_the_untouched_bundle():
    w = World()
    d = bundle(w)
    assert verify_record(d) == []
    assert verify_record(d, expected_envelope_digest=d["envelope_digest"]) == []


def test_verify_names_the_row_when_one_executed_value_is_edited():
    d = bundle(World())
    bad = copy.deepcopy(d)
    next(r for r in bad["rows"] if r["field"] == "rate")["executed"] = "1.20"
    problems = verify_record(bad)
    assert any(p.startswith("RATE_MISMATCH") for p in problems)
    assert any("record_digest" in p.lower() or "DIGEST" in p for p in problems)


def test_verify_catches_an_edit_made_consistently_to_both_columns():
    d = bundle(World())
    bad = copy.deepcopy(d)
    r = next(r for r in bad["rows"] if r["field"] == "rate")
    r["approved"] = r["executed"] = "1.20"
    assert verify_record(bad)  # approved column no longer equals the bound envelope


def test_verify_catches_a_fully_recomputed_bundle_only_with_the_independent_digest():
    d = bundle(World())
    bad = copy.deepcopy(d)
    bad["envelope"]["quote"]["client_rate"] = "1.20"
    bad["provider"]["conversion"]["client_rate"] = "1.20"
    for r in bad["rows"]:
        if r["field"] == "rate":
            r["approved"] = r["executed"] = "1.20"
    from rate_locked.record import envelope_digest_of, record_digest_of
    bad["envelope_digest"] = envelope_digest_of(bad["envelope"])
    bad["record_digest"] = record_digest_of(bad)
    assert verify_record(bad) == []  # internally consistent: a copy alone cannot prove authenticity
    problems = verify_record(bad, expected_envelope_digest=d["envelope_digest"])
    assert any("ENVELOPE_DIGEST" in p for p in problems)


# ---- beneficiary provenance (package review R12) -----------------------------------------------------

def test_a_beneficiary_the_reply_did_not_name_is_marked_as_request_context_not_a_provider_echo():
    from rate_locked.awx.wire import transfer_from_wire
    base = {"id": "t1", "request_id": "r1", "status": "PAID", "source_currency": "GBP", "transfer_currency": "GBP",
            "transfer_amount": "1000.00"}
    assert transfer_from_wire({**base, "beneficiary_id": "b1"}, "b1").beneficiary_from == "reply"
    t = transfer_from_wire(base, "b1")
    assert t.beneficiary_id == "b1" and t.beneficiary_from == "request"


def test_the_record_and_the_console_say_where_the_beneficiary_value_came_from():
    from dataclasses import replace
    from pathlib import Path
    from rate_locked.record import _provider_dict
    w = World()
    ex = w.executor.dispatch(w.approved("O2").execution_id)
    assert "beneficiary_from" not in _provider_dict(ex)["transfer"]         # an echoed id keeps the old record shape
    ex.transfer = replace(ex.transfer, beneficiary_from="request")
    assert _provider_dict(ex)["transfer"]["beneficiary_from"] == "request"
    web = (Path(__file__).resolve().parents[1] / "src" / "rate_locked" / "web" / "console.js").read_text(encoding="utf-8")
    assert "from this app's own request: the provider's reply named no beneficiary" in web

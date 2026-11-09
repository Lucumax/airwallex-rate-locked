"""Invoice reader: the model reads, code decides. Verbatim source words, offsets computed by code, review flags.

The only reader in this repository is a TEST DOUBLE (scripted, no model). These tests pin the contract a live
model reader must meet later: quotes found verbatim, values consistent with their quotes, no decision field,
stop reasons `max_tokens` and `refusal` rejected to review.
"""
import ast
from datetime import datetime, timezone
from decimal import Decimal as D
from pathlib import Path

from rate_locked.invoices import INVOICES
from rate_locked.reader import (FIELDS, ScriptedTestReader, check_explanation, read_invoice, validate_reply)
from rate_locked.scenario import demo_inputs

SRC = Path(__file__).resolve().parents[1] / "src" / "rate_locked"

TEXT = ("Harbor Tools\nINVOICE HT-77\nAmount due: EUR 1,250.50\nDue by: 4 November 2026, 12:00 UTC\n")


def reply(**fields):
    base = {
        "supplier": [{"value": "Harbor Tools", "quote": "Harbor Tools"}],
        "invoice_number": [{"value": "HT-77", "quote": "INVOICE HT-77"}],
        "amount": [{"value": "1250.50", "quote": "Amount due: EUR 1,250.50"}],
        "currency": [{"value": "EUR", "quote": "EUR 1,250.50"}],
        "due_date": [{"value": "2026-11-04T12:00:00+00:00", "quote": "Due by: 4 November 2026, 12:00 UTC"}],
    }
    base.update(fields)
    return {"reader": "test", "stop_reason": "end_turn", "fields": base}


def test_clean_reply_is_ready_with_offsets_computed_by_code():
    ex = validate_reply(TEXT, reply())
    assert ex.ready and ex.flags == ()
    amt = ex.field("amount")
    assert amt.value == D("1250.50") and amt.status == "OK"
    assert TEXT[amt.start:amt.end] == "Amount due: EUR 1,250.50"
    assert ex.field("due_date").value == datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
    assert set(f.name for f in ex.fields) == set(FIELDS)


def test_quote_not_found_verbatim_is_rejected_to_review():
    ex = validate_reply(TEXT, reply(amount=[{"value": "1250.50", "quote": "Amount due EUR 1250.50"}]))
    assert not ex.ready
    f = ex.field("amount")
    assert f.status == "REVIEW" and "QUOTE_NOT_VERBATIM" in f.flags and f.start is None


def test_value_must_be_supported_by_its_quote():
    ex = validate_reply(TEXT, reply(amount=[{"value": "1520.50", "quote": "Amount due: EUR 1,250.50"}]))
    assert "VALUE_NOT_IN_QUOTE" in ex.field("amount").flags and not ex.ready
    ex = validate_reply(TEXT, reply(due_date=[{"value": "2026-11-05T12:00:00+00:00",
                                               "quote": "Due by: 4 November 2026, 12:00 UTC"}]))
    assert "VALUE_NOT_IN_QUOTE" in ex.field("due_date").flags


def test_missing_and_conflicting_fields_are_flagged():
    r = reply(currency=[])
    del r["fields"]["supplier"]
    ex = validate_reply(TEXT, r)
    assert ex.field("currency").flags == ("MISSING",) and ex.field("supplier").flags == ("MISSING",)
    ex = validate_reply(TEXT, reply(amount=[{"value": "1250.50", "quote": "Amount due: EUR 1,250.50"},
                                            {"value": "1250.00", "quote": "EUR 1,250.50"}]))
    assert "CONFLICT" in ex.field("amount").flags and not ex.ready


def test_unparseable_values_go_to_review():
    ex = validate_reply(TEXT, reply(amount=[{"value": "about a thousand", "quote": "Amount due: EUR 1,250.50"}],
                                    due_date=[{"value": "next week", "quote": "Due by: 4 November 2026, 12:00 UTC"}]))
    assert "UNREADABLE_VALUE" in ex.field("amount").flags
    assert "UNREADABLE_VALUE" in ex.field("due_date").flags


def test_reader_cannot_carry_a_decision():
    ex = validate_reply(TEXT, reply(action=[{"value": "FUND", "quote": "Harbor Tools"}]))
    assert not ex.ready and any(f.startswith("UNEXPECTED_FIELD") for f in ex.flags)
    r = reply()
    r["decision"] = "CONVERT"
    ex = validate_reply(TEXT, r)
    assert not ex.ready and "UNEXPECTED_KEY decision" in ex.flags


def test_stop_reasons_max_tokens_and_refusal_are_rejections():
    for reason in ("max_tokens", "refusal"):
        r = reply()
        r["stop_reason"] = reason
        ex = validate_reply(TEXT, r)
        assert not ex.ready and f"READER_STOPPED {reason}" in ex.flags


def test_test_double_is_labelled_and_reads_the_three_fixture_invoices():
    reader = ScriptedTestReader()
    assert reader.is_model is False
    assert "TEST DOUBLE" in reader.label
    seed = {o.invoice_ref: o for o in demo_inputs().obligations}
    assert len(INVOICES) == 3
    for inv in INVOICES:
        ex = read_invoice(reader, inv)
        if inv.invoice_id == REVIEW_INVOICE:
            continue  # pinned by test_one_fixture_invoice_goes_to_review
        assert ex.ready, (inv.invoice_id, ex.flags, [f.flags for f in ex.fields])
        ob = seed[ex.field("invoice_number").value]
        assert ex.field("supplier").value == ob.supplier_name
        assert ex.field("amount").value == ob.amount and ex.field("currency").value == ob.currency
        assert ex.field("due_date").value == ob.due_at
        assert inv.obligation_id == ob.obligation_id
        for f in ex.fields:
            assert inv.text[f.start:f.end] == f.quote


REVIEW_INVOICE = "inv-initech"


def test_one_fixture_invoice_goes_to_review():
    """The Initech invoice gives a payment time with no time zone. The TEST DOUBLE assumes UTC; code refuses the
    assumption and flags the field, so a person has to decide before that invoice can reach the planner."""
    reader = ScriptedTestReader()
    by_id = {inv.invoice_id: read_invoice(reader, inv) for inv in INVOICES}
    assert [i for i, ex in by_id.items() if not ex.ready] == [REVIEW_INVOICE]
    ex = by_id[REVIEW_INVOICE]
    due = ex.field("due_date")
    assert due.status == "REVIEW" and due.flags == ("TIMEZONE_NOT_STATED",)
    assert due.quote == "Payment due: 4 November 2026, 21:00"
    assert "UTC" not in next(i for i in INVOICES if i.invoice_id == REVIEW_INVOICE).text
    assert all(f.status == "OK" for f in ex.fields if f.name != "due_date") and ex.flags == ()


def test_fixture_invoices_are_fictional():
    for inv in INVOICES:
        for line in inv.text.splitlines():
            if "@" in line:
                assert line.strip().endswith(".example"), line
        assert ".com" not in inv.text.lower(), inv.invoice_id


GENERIC = {"Acme Co.", "Globex Ltd.", "Initech Pte. Ltd."}


def test_fixture_suppliers_use_generic_placeholder_names():
    assert {o.supplier_name for o in demo_inputs().obligations} == GENERIC
    for inv in INVOICES:
        assert inv.text.splitlines()[0].strip().lower().rstrip(".") in {n.lower().rstrip(".") for n in GENERIC}
    assert {r.payer for r in demo_inputs().receipts} == {"Vandelay Industries"}


def test_explanation_numbers_must_come_from_the_facts():
    facts = ["O1 Acme Co. FUND: The EUR balance covers EUR 1,200.00."]
    assert check_explanation("Acme Co. is paid from the EUR balance (EUR 1,200.00).", facts) == []
    bad = check_explanation("Acme Co. is paid EUR 1,300.00 from the EUR balance.", facts)
    assert bad and "1,300.00" in bad[0]


def test_test_double_explanation_passes_its_own_check():
    reader = ScriptedTestReader()
    facts = ["O1 Acme Co. FUND: The EUR balance covers EUR 1,200.00.",
             "O2 Globex Ltd. CONVERT: GBP 1,000.00 short."]
    out = reader.explain(facts)
    assert out["stop_reason"] == "end_turn"
    assert check_explanation(out["text"], facts) == []


def test_reader_module_makes_no_network_or_model_calls():
    tree = ast.parse((SRC / "reader.py").read_text(encoding="utf-8"))
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            mods.add(node.module.split(".")[0])
    assert not mods & {"urllib", "http", "socket", "requests", "httpx", "anthropic", "openai", "ssl"}, mods


def test_decision_code_does_not_import_the_reader():
    for name in ("planner.py", "approval.py", "executor.py", "record.py", "models.py"):
        tree = ast.parse((SRC / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert "reader" not in node.module and "invoices" not in node.module, (name, node.module)


def test_every_candidate_is_checked_not_only_the_first():
    good = {"value": "1250.50", "quote": "Amount due: EUR 1,250.50"}
    invented = {"value": "1250.50", "quote": "Amount due: EUR 1,250.50 (agreed)"}  # same value, quote not in the text
    ex = validate_reply(TEXT, reply(amount=[good, invented]))
    assert not ex.ready and "QUOTE_NOT_VERBATIM" in ex.field("amount").flags
    ex = validate_reply(TEXT, reply(amount=[good, {"value": "not a number", "quote": "Amount due: EUR 1,250.50"}]))
    assert not ex.ready and "UNREADABLE_VALUE" in ex.field("amount").flags

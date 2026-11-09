"""Adapter, probe and error-body handling findings from an independent review, one test each (F-numbers).
F1 (restart) is in test_restart_guard.py."""
import importlib.util
import json
import sys
from decimal import Decimal as D
from pathlib import Path

import pytest

from rate_locked.awx.adapter import AirwallexError, UncertainOutcome
from rate_locked.awx.http_adapter import HttpAdapter
from rate_locked.executor import State
from rate_locked.reader import validate_reply
from replay_server import ReplayServer, fixture

from helpers import World

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("sandbox_probe", ROOT / "scripts" / "sandbox_probe.py")
probe = importlib.util.module_from_spec(spec)
sys.modules.setdefault("sandbox_probe", probe)
spec.loader.exec_module(probe)

CID, KEY = "client-id-SENTINEL-1234", "api-key-SENTINEL-abcdefghijkl"
IBAN = "DE00SENTINELIBAN1234567"


def adapter(srv):
    return HttpAdapter(srv.base_url, "id-for-tests", "key-for-tests", timeout=5)


def create_transfer(a):
    return a.create_transfer(request_id="r", beneficiary_id="ben-1", source_currency="EUR", transfer_currency="EUR",
                             transfer_amount=D("1"), reason="x", reference="y")


def create_conversion(a):
    return a.create_conversion(request_id="r", quote_id="q", buy_currency="GBP", sell_currency="USD", buy_amount=D("1"))


# ---- F2: an unreadable answer to a create is UNCERTAIN, never a failure -----------------------------------------------
@pytest.mark.parametrize("body", ["", "not json at all", '{"code":"request_id_dup', "[]", "{}", '{"message":"no code"}'])
@pytest.mark.parametrize("status", [400, 409])
@pytest.mark.parametrize("path,make", [("/transfers/create", create_transfer), ("/fx/conversions/create", create_conversion)])
def test_f2_unreadable_or_unclassified_create_rejection_is_uncertain(path, make, status, body):
    with ReplayServer() as srv:
        srv.on("POST", path, body, status)
        with pytest.raises(UncertainOutcome):
            make(adapter(srv))
        assert len(srv.sent("POST", path)) == 1  # and never re-sent


def test_f2_a_recognised_rejection_is_still_a_definite_failure():
    with ReplayServer() as srv:
        srv.on("POST", "/transfers/create", '{"code":"insufficient_funds","message":"x"}', 400)
        with pytest.raises(AirwallexError) as e:
            create_transfer(adapter(srv))
        assert e.value.code == "insufficient_funds"


def test_f2_a_read_with_an_unreadable_error_body_is_still_an_ordinary_error():
    with ReplayServer() as srv:
        srv.on("GET", "/transfers/t1", "not json", 400)
        with pytest.raises(AirwallexError):
            adapter(srv).get_transfer("t1")


def test_f2_executor_never_marks_a_possibly_live_payment_failed_on_an_unreadable_duplicate_answer():
    with ReplayServer() as srv:
        srv.on("POST", "/transfers/create", '{"code":"request_id_du', 400)
        srv.on("GET", "/transfers", '{"items": [], "page_after": null}')
        srv.on("GET", "/balances/current", fixture("balances.json"))
        w = World()
        w.executor.calls._inner = adapter(srv)  # the real adapter against the replay double
        ex = w.approved("O1")
        w.executor.dispatch(ex.execution_id)
        assert ex.state is State.UNKNOWN and ex.unknown_stage == "payout"


# ---- F9: polling must not erase the beneficiary the transfer was created for ------------------------------------------------
def test_f9_get_transfer_keeps_the_beneficiary_the_create_established():
    with ReplayServer() as srv:
        srv.on("POST", "/transfers/create", fixture("transfer_create.json"), 201)
        srv.on("GET", "/transfers/trf-doc-0001", json.dumps({**json.loads(fixture("transfer_create.json")), "status": "SENT"}))
        a = adapter(srv)
        t = a.create_transfer(request_id="req-doc-2", beneficiary_id="ben-1", source_currency="EUR",
                              transfer_currency="EUR", transfer_amount=D("1200.00"), reason="x", reference="y")
        assert t.beneficiary_id == "ben-1"
        assert a.get_transfer(t.transfer_id).beneficiary_id == "ben-1"


def test_f9_an_explicit_conflicting_beneficiary_in_the_answer_is_still_visible():
    with ReplayServer() as srv:
        srv.on("POST", "/transfers/create", fixture("transfer_create.json"), 201)
        srv.on("GET", "/transfers/trf-doc-0001",
               json.dumps({**json.loads(fixture("transfer_create.json")), "beneficiary_id": "someone-else"}))
        a = adapter(srv)
        t = a.create_transfer(request_id="req-doc-2", beneficiary_id="ben-1", source_currency="EUR",
                              transfer_currency="EUR", transfer_amount=D("1200.00"), reason="x", reference="y")
        assert a.get_transfer(t.transfer_id).beneficiary_id == "someone-else"


# ---- F5: provider error text never carries secrets or account numbers into exceptions ---------------------------------------
def test_f5_provider_error_messages_are_scrubbed_and_bounded():
    msg = (f"Invalid iban {IBAN} for account 12345678901234 token tok-doc-not-a-secret key key-for-tests "
           f"{'x' * 600}")
    with ReplayServer() as srv:
        srv.on("POST", "/beneficiaries/create", json.dumps({"code": "validation_error", "message": msg}), 400)
        with pytest.raises(AirwallexError) as e:
            adapter(srv).create_beneficiary(request_id="r", payload={"name": "x"})
    for text in (str(e.value), e.value.message):
        assert IBAN not in text and "12345678901234" not in text and "tok-doc-not-a-secret" not in text
        assert "key-for-tests" not in text and len(text) < 260
    assert e.value.code == "validation_error"


# ---- F7: the amount in the quote must be the WHOLE number, with exact value ---------------------------------------------
ACME = "Amount due: EUR 1,200.00"


def reply(**over):
    fields = {"supplier": [{"value": "Acme Co.", "quote": "Acme Co."}],
              "invoice_number": [{"value": "INV-1001", "quote": "INVOICE INV-1001"}],
              "amount": [{"value": "1200.00", "quote": ACME}],
              "currency": [{"value": "EUR", "quote": "EUR 1,200.00"}],
              "due_date": [{"value": "2026-11-03T05:00:00+00:00", "quote": "Due by: 3 November 2026, 05:00 UTC"}]}
    fields.update(over)
    return {"reader": "t", "stop_reason": "end_turn", "fields": fields}


def text_of(invoice_id):
    from rate_locked.invoices import INVOICES
    return next(i.text for i in INVOICES if i.invoice_id == invoice_id)


def test_f7_baseline_reply_is_ready():
    assert validate_reply(text_of("inv-acme"), reply()).ready


@pytest.mark.parametrize("value", ["200.00", "1200.004", "1.20", "20", "1,200.00", 1200.0, 1200, True])
def test_f7_an_amount_that_is_not_the_whole_quoted_number_is_flagged(value):
    ex = validate_reply(text_of("inv-acme"), reply(amount=[{"value": value, "quote": ACME}]))
    assert not ex.ready and ex.field("amount").flags


def test_f7_an_amount_inside_a_longer_number_does_not_pass():
    ex = validate_reply(text_of("inv-acme"), reply(amount=[{"value": "200.00", "quote": "EUR 1,200.00"}]))
    assert "VALUE_NOT_IN_QUOTE" in ex.field("amount").flags


def test_f7_non_string_values_and_quotes_are_flagged_not_coerced():
    ex = validate_reply(text_of("inv-acme"), reply(currency=[{"value": 123, "quote": "EUR 1,200.00"}],
                                                   supplier=[{"value": "Acme Co.", "quote": ["Acme Co."]}]))
    assert not ex.ready


# ---- F8: the due date must match the quoted day, time and stated zone ---------------------------------------------------
def due(value, quote="Due by: 3 November 2026, 05:00 UTC"):
    return reply(due_date=[{"value": value, "quote": quote}])


@pytest.mark.parametrize("value", ["2026-11-03T05:00:00+14:00", "2026-11-03T05:00:00-05:00"])
def test_f8_an_offset_that_contradicts_the_stated_utc_is_flagged(value):
    ex = validate_reply(text_of("inv-acme"), due(value))
    assert not ex.ready and "TIMEZONE_MISMATCH" in ex.field("due_date").flags


def test_f8_a_zone_less_time_cannot_be_hidden_behind_midnight():
    ex = validate_reply(text_of("inv-initech"), due("2026-11-04T00:00:00+00:00", "Payment due: 4 November 2026, 21:00"))
    assert not ex.ready and ex.field("due_date").flags


def test_f8_a_zone_less_time_is_still_held_for_review():
    ex = validate_reply(text_of("inv-initech"), due("2026-11-04T21:00:00+00:00", "Payment due: 4 November 2026, 21:00"))
    assert "TIMEZONE_NOT_STATED" in ex.field("due_date").flags


def test_f8_the_day_must_be_the_quoted_day_not_any_number_in_the_quote():
    ex = validate_reply(text_of("inv-acme"), due("2026-11-05T05:00:00+00:00"))  # "05" of 05:00 is not the day
    assert not ex.ready and "VALUE_NOT_IN_QUOTE" in ex.field("due_date").flags


# ---- model reader: strict local schema before anything is transformed ---------------------------------------------------
def _model(payload):
    from types import SimpleNamespace as NS
    from rate_locked.model_reader import ModelInvoiceReader

    class C:
        messages = NS(create=lambda **kw: NS(stop_reason="end_turn", content=[NS(type="text", text=json.dumps(payload))],
                                             usage=None))
    return ModelInvoiceReader(C())


@pytest.mark.parametrize("mutate", [
    lambda p: p["amount"][0].update(value=1200.0),                 # numeric amount instead of a string
    lambda p: p["amount"][0].update(extra="x"),                    # extra key on a candidate
    lambda p: p["amount"][0].pop("quote"),                         # missing key
    lambda p: p.update(decision="pay now"),                        # extra top-level field
])
def test_model_reply_must_match_the_declared_schema_exactly(mutate):
    from rate_locked.invoices import SCRIPTED_REPLIES
    payload = {k: [dict(c) for c in v] for k, v in SCRIPTED_REPLIES["inv-acme"].items()}
    mutate(payload)
    out = _model(payload).extract("inv-acme", text_of("inv-acme"))
    assert out["stop_reason"] == "unparseable" and out["fields"] == {}


# ---- F4: free-text echoes of private beneficiary values never reach evidence or output --------------------------------------
def test_f4_an_error_message_echoing_the_iban_is_redacted_in_evidence_and_output(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AIRWALLEX_CLIENT_ID", CID)
    monkeypatch.setenv("AIRWALLEX_API_KEY", KEY)
    entry = {"entity_type": "COMPANY", "company_name": "Acme Co.",
             "bank_details": {"account_currency": "EUR", "account_name": "Acme Co.", "bank_country_code": "DE",
                              "iban": IBAN, "account_number": "98765432109876"}}
    bens = tmp_path / "bens.json"
    bens.write_text(json.dumps({"Acme Co.": entry}), encoding="utf-8")
    with ReplayServer() as srv:
        srv.on("POST", "/beneficiaries/create",
               json.dumps({"code": "validation_error", "message": f"Invalid iban {IBAN}; account 98765432109876"}), 400)
        probe.main(["--base-url", srv.base_url, "--out", str(tmp_path / "ev"), "--payout", "--beneficiaries", str(bens)],
                   sleep=lambda s: None)
    out = capsys.readouterr().out
    text = "".join(p.read_text() for p in (tmp_path / "ev").glob("*.json"))
    assert text, "an evidence file should exist"
    for secret in (IBAN, "98765432109876", KEY, CID):
        assert secret not in out and secret not in text


# ---- F10: a conversion create answered duplicate or pending is looked up, not forgotten ------------------------------------
@pytest.mark.parametrize("status,code", [(400, "request_id_duplicate"), (409, "request_pending")])
def test_f10_probe_conversion_duplicate_or_pending_is_uncertain_and_looked_up(tmp_path, monkeypatch, status, code):
    monkeypatch.setenv("AIRWALLEX_CLIENT_ID", CID)
    monkeypatch.setenv("AIRWALLEX_API_KEY", KEY)
    with ReplayServer() as srv:
        srv.on("POST", "/fx/quotes/create", fixture("quote.json"), 201)
        srv.on("POST", "/fx/conversions/create", json.dumps({"code": code, "message": "x"}), status)
        srv.on("GET", "/fx/conversions", '{"items": [], "has_more": false}')
        probe.main(["--base-url", srv.base_url, "--out", str(tmp_path), "--convert"], sleep=lambda s: None)
        ev = json.loads(next(tmp_path.glob("probe_*.json")).read_text())
        assert len(srv.sent("POST", "/fx/conversions/create")) == 1
    p7 = next(s for s in ev["steps"] if s["id"] == "P7")
    assert p7.get("uncertain") is True
    assert any(s["id"] == "P9" for s in ev["steps"])


# ---- F6: the client id is typed hidden too ---------------------------------------------------------------------------------
def test_f6_first_run_reads_the_client_id_without_echo():
    text = (ROOT / "scripts" / "first_run.ps1").read_text(encoding="utf-8")
    assert 'Read-Host "Airwallex sandbox Client ID"' not in text
    assert 'Read-Secret "Airwallex sandbox Client ID"' in text


# ---- sandbox start-up and shapecheck: no raw provider text, no raw values --------------------------------------------------
def test_startup_failure_creating_a_beneficiary_is_a_printable_setup_error_without_provider_text(tmp_path):
    from rate_locked.sandbox import SandboxSetupError, build_sandbox_session, supplier_names
    good = {"bank_details": {"account_currency": "EUR", "account_name": "T", "bank_country_code": "DE", "iban": IBAN},
            "entity_type": "COMPANY", "company_name": "T"}
    bens = tmp_path / "b.json"
    bens.write_text(json.dumps({n: good for n in supplier_names()}), encoding="utf-8")
    with ReplayServer() as srv:
        srv.on("GET", "/balances/current", fixture("balances.json"))
        srv.on("POST", "/beneficiaries/create", json.dumps({"code": "validation_error", "message": f"bad {IBAN}"}), 400)
        with pytest.raises(SandboxSetupError) as e:
            build_sandbox_session(env={"AIRWALLEX_CLIENT_ID": CID, "AIRWALLEX_API_KEY": KEY}, base_url=srv.base_url,
                                  beneficiaries_path=str(bens))
    assert IBAN not in str(e.value) and "validation_error" in str(e.value)
    assert e.value.__cause__ is None and e.value.__suppress_context__


def test_shapecheck_detail_never_repeats_raw_values():
    from rate_locked.shapecheck import check_evidence
    step = {"id": "P12", "ok": True, "http_status": 201,
            "response": {"id": "t", "request_id": "r", "status": "PROCESSING", "source_currency": "EUR",
                         "transfer_currency": "EUR", "transfer_amount": "1.00", "created_at": "SENTINEL-" + IBAN}}
    rows = check_evidence({"steps": [step]})
    assert all(IBAN not in r.detail for r in rows)

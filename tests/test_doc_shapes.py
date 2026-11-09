"""The adapter against the DOCUMENTED Airwallex shapes (hand-written fixtures from the public reference, read
2026-11-01; see tests/fixtures/awx_docs/README.md for the page behind each one). These are NOT sandbox recordings.

The first version of wire.py assumed shapes (beneficiary_id, top-level rate amounts, `transition`, JSON-number
amounts, has_more on every list). Each test below pins one place where the reference says otherwise.
"""
from datetime import datetime, timezone
from decimal import Decimal as D

import pytest

from rate_locked.awx import wire
from rate_locked.awx.adapter import AirwallexError, means_lookup
from rate_locked.awx.http_adapter import HttpAdapter
from replay_server import ReplayServer, fixture


def adapter(srv):
    return HttpAdapter(srv.base_url, "id-for-tests", "key-for-tests", timeout=5)


# ---- parsers on documented bodies -------------------------------------------------------------------------

def test_balances_count_cash_accounts_only():
    bal = {b.currency: b.available for b in wire.balances_from_wire(wire.loads(fixture("balances.json")))}
    # the yield USD 99,999 and the credit EUR 5,000 lines are not spendable cash
    assert bal == {"USD": D("3000"), "EUR": D("1500.5"), "GBP": D("0")}


def test_balance_without_account_type_is_treated_as_cash():
    assert wire.balances_from_wire([{"currency": "USD", "available_amount": 5}])[0].available == D("5")


def test_rate_amounts_come_from_the_client_level_of_rate_details():
    r = wire.rate_from_wire(wire.loads(fixture("rate_current.json")))
    assert (r.sell_currency, r.buy_currency, r.currency_pair) == ("USD", "GBP", "GBPUSD")
    assert (r.sell_amount, r.buy_amount, r.rate) == (D("1300"), D("1000"), D("1.3"))
    assert r.created_at == datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)


def test_rate_without_a_client_level_is_unusable_not_guessed():
    d = wire.loads(fixture("rate_current.json"))
    d["rate_details"] = [x for x in d["rate_details"] if x["level"] != "CLIENT"]
    with pytest.raises((KeyError, ValueError)):
        wire.rate_from_wire(d)


def test_quote_documented_shape():
    q = wire.quote_from_wire(wire.loads(fixture("quote.json")))
    assert q.quote_id == "qte-doc-0001" and q.client_rate == D("1.3") and q.usage == "SINGLE_USE"
    assert q.valid_to_at.isoformat() == "2026-11-02T09:01:00+00:00"


def test_conversion_documented_shape_with_string_amounts():
    c = wire.conversion_from_wire(wire.loads(fixture("conversion_create.json")))
    assert (c.conversion_id, c.status, c.buy_amount, c.sell_amount) == ("cnv-doc-0001", "SCHEDULED", D("1000.00"), D("1300.00"))


def test_beneficiary_identifier_is_id():
    b = wire.beneficiary_from_wire(wire.loads(fixture("beneficiary_create.json")), request_id="r-ben")
    assert b.beneficiary_id == "8365ea88-2946-4877-abe1-e02a1015288d" and b.request_id == "r-ben"
    assert "account_number" not in str(b.summary)


def test_transfer_without_a_beneficiary_id_takes_the_one_we_sent():
    d = wire.loads(fixture("transfer_create.json"))
    assert "beneficiary_id" not in d
    t = wire.transfer_from_wire(d, beneficiary_id="ben-1")
    assert (t.transfer_id, t.status, t.beneficiary_id, t.transfer_amount) == ("trf-doc-0001", "PROCESSING", "ben-1", D("1200.00"))


def test_transfer_without_any_beneficiary_is_empty_string_not_invented():
    assert wire.transfer_from_wire(wire.loads(fixture("transfer_create.json"))).beneficiary_id == ""


def test_transfer_beneficiary_may_be_nested():
    d = wire.loads(fixture("transfer_create.json"))
    d["beneficiary"] = {"id": "ben-nested"}
    assert wire.transfer_from_wire(d).beneficiary_id == "ben-nested"


def test_deposit_documented_shape_uses_the_request_id_we_hold():
    x = wire.deposit_from_wire(wire.loads(fixture("deposit.json")), request_id="dep-1")
    assert (x.deposit_id, x.currency, x.amount, x.status, x.request_id) == ("dep-doc-0001", "USD", D("1000"), "SETTLED", "dep-1")


def test_global_account_for_picks_an_active_account_that_accepts_deposits_in_the_currency():
    data = wire.loads(fixture("global_accounts.json"))
    assert wire.global_account_for(data["items"], "USD") == "ga-doc-0001"
    assert wire.global_account_for(data["items"], "JPY") is None


# ---- request bodies -------------------------------------------------------------------------------------------

def test_conversion_body_follows_the_reference():
    body = wire.conversion_request_body(request_id="r1", quote_id="q1", buy_currency="GBP", sell_currency="USD",
                                        buy_amount=D("1000.00"), sell_amount=None)
    assert body == {"request_id": "r1", "quote_id": "q1", "buy_currency": "GBP", "sell_currency": "USD",
                    "buy_amount": "1000.00"}  # strings; no `reason`, no `term_agreement` (not in the reference)


def test_transfer_body_follows_the_reference():
    body = wire.transfer_request_body(request_id="r2", beneficiary_id="b1", source_currency="EUR",
                                      transfer_currency="EUR", transfer_amount=D("1200.00"), reason="supplier payment",
                                      reference="INV-1001", transfer_method="LOCAL")
    assert body["transfer_amount"] == "1200.00" and body["source_currency"] == "EUR"
    assert body["beneficiary_id"] == "b1" and body["reason"] and body["reference"] and body["request_id"] == "r2"
    assert "source_amount" not in body  # the dealt side is the transfer amount


def test_transition_body_uses_next_status():
    assert wire.transition_body("PAID") == {"next_status": "PAID"}


def test_deposit_body_uses_the_global_account():
    assert wire.deposit_request_body(global_account_id="ga-1", amount=D("1000")) == {"global_account_id": "ga-1", "amount": D("1000")}


# ---- the adapter against a replay of those shapes ---------------------------------------------------------------

def test_create_conversion_sends_string_amounts_and_no_undocumented_fields():
    with ReplayServer() as srv:
        srv.on("POST", "/fx/conversions/create", fixture("conversion_create.json"), 201)
        c = adapter(srv).create_conversion(request_id="req-doc-1", quote_id="qte-doc-0001", buy_currency="GBP",
                                           sell_currency="USD", buy_amount=D("1000.00"), reason="INV supplier payment")
        sent = srv.sent("POST", "/fx/conversions/create")[0]
        assert '"buy_amount":"1000.00"' in sent.body_text.replace(" ", "")
        assert "term_agreement" not in sent.body_text and "reason" not in sent.body_text
        assert c.conversion_id == "cnv-doc-0001"
        assert "x-api-version" not in {k.lower() for k in sent.headers}


def test_create_transfer_fills_the_beneficiary_it_sent_and_sends_strings():
    with ReplayServer() as srv:
        srv.on("POST", "/transfers/create", fixture("transfer_create.json"), 201)
        t = adapter(srv).create_transfer(request_id="req-doc-2", beneficiary_id="ben-1", source_currency="EUR",
                                         transfer_currency="EUR", transfer_amount=D("1200.00"),
                                         reason="supplier payment", reference="INV-1001")
        assert t.beneficiary_id == "ben-1" and t.status == "PROCESSING"
        assert '"transfer_amount":"1200.00"' in srv.sent("POST", "/transfers/create")[0].body_text.replace(" ", "")


def test_create_beneficiary_reads_id():
    with ReplayServer() as srv:
        srv.on("POST", "/beneficiaries/create", fixture("beneficiary_create.json"), 201)
        b = adapter(srv).create_beneficiary(request_id="r-ben", payload={"type": "BANK_ACCOUNT"})
        assert b.beneficiary_id.startswith("8365ea88")


@pytest.mark.parametrize("op,path,status,code", [
    ("conversion", "/fx/conversions/create", 400, "request_id_duplicate"),
    ("conversion", "/fx/conversions/create", 409, "request_pending"),
    ("transfer", "/transfers/create", 400, "request_id_duplicate"),
    ("transfer", "/transfers/create", 409, "request_pending"),
])
def test_duplicate_and_pending_codes_pass_through_verbatim_and_are_never_resent(op, path, status, code):
    """The reference documents request_id_duplicate (400) and request_pending (409). The adapter reports them
    verbatim (the executor decides: see the next tests) and never re-sends."""
    with ReplayServer() as srv:
        srv.on("POST", path, '{"code":"%s","message":"x"}' % code, status)
        a = adapter(srv)
        with pytest.raises(AirwallexError) as e:
            if op == "conversion":
                a.create_conversion(request_id="r", quote_id="q", buy_currency="GBP", sell_currency="USD", buy_amount=D("1"))
            else:
                a.create_transfer(request_id="r", beneficiary_id="b", source_currency="EUR", transfer_currency="EUR",
                                  transfer_amount=D("1"), reason="x", reference="y")
        assert e.value.code == code and e.value.http_status == status and means_lookup(e.value)
        assert len(srv.sent("POST", path)) == 1


def test_means_lookup_is_only_for_duplicate_or_pending():
    assert means_lookup(AirwallexError("request_pending", "", 409))
    assert means_lookup(AirwallexError("anything", "", 409))
    assert means_lookup(AirwallexError("request_id_duplicate", "", 400))
    assert not means_lookup(AirwallexError("validation_failed", "", 400))
    assert not means_lookup(AirwallexError("expired_quote", "", 400))


def test_executor_treats_a_duplicate_transfer_request_as_unknown_then_finds_it():
    """If the provider says the transfer request id already exists, the payment may be live: never FAILED."""
    from helpers import World
    from rate_locked.executor import State
    w = World()
    ex = w.approved("O1")  # O1 pays from the existing balance: no conversion first
    real = w.fake.create_transfer
    state = {"first": True}

    def dup_then_real(**kw):
        if state["first"]:
            state["first"] = False
            real(**kw)  # the provider did create it (an earlier send whose answer we never saw)
            raise AirwallexError("request_id_duplicate", "already exists", 400)
        return real(**kw)

    w.fake.create_transfer = dup_then_real
    out = w.executor.dispatch(ex.execution_id)
    assert out.state is State.UNKNOWN and out.unknown_stage == "payout"
    out = w.executor.reconcile(ex.execution_id)
    assert out.transfer is not None and out.state is not State.PAYOUT_FAILED


def test_executor_treats_a_pending_conversion_request_as_unknown():
    from helpers import World
    from rate_locked.executor import State
    w = World()
    ex = w.approved("O2")
    w.fake.create_conversion = lambda **kw: (_ for _ in ()).throw(AirwallexError("request_pending", "in flight", 409))
    out = w.executor.dispatch(ex.execution_id)
    assert out.state is State.UNKNOWN and out.unknown_stage == "conversion"


def test_a_plain_validation_error_is_still_a_definite_refusal():
    with ReplayServer() as srv:
        srv.on("POST", "/transfers/create", '{"code":"validation_failed","message":"x"}', 400)
        with pytest.raises(AirwallexError) as e:
            adapter(srv).create_transfer(request_id="r", beneficiary_id="b", source_currency="EUR",
                                         transfer_currency="EUR", transfer_amount=D("1"), reason="x", reference="y")
        assert e.value.code == "validation_failed"


def test_find_transfers_follows_the_page_bookmark_and_filters_by_beneficiary():
    with ReplayServer() as srv:
        srv.on("GET", "/transfers", '{"items": [], "page_after": "bookmark-2", "page_before": null}')
        srv.on("GET", "/transfers", fixture("transfer_list.json"))
        found = adapter(srv).find_transfers("req-doc-2", beneficiary_id="ben-1")
        assert [t.transfer_id for t in found] == ["trf-doc-0001"]
        assert found[0].beneficiary_id == "ben-1"
        reqs = srv.sent("GET", "/transfers")
        assert reqs[0].query["request_id"] == "req-doc-2" and reqs[0].query["beneficiary_id"] == "ben-1"
        assert reqs[1].query["page"] == "bookmark-2"


def test_find_transfers_gives_up_loudly_if_pages_never_end():
    with ReplayServer() as srv:
        srv.on("GET", "/transfers", '{"items": [], "page_after": "again"}')
        with pytest.raises(AirwallexError) as e:
            adapter(srv).find_transfers("r")
        assert e.value.code == "incomplete_lookup"


def test_find_conversions_follows_has_more_with_page_num():
    with ReplayServer() as srv:
        srv.on("GET", "/fx/conversions", '{"items": [], "has_more": true}')
        srv.on("GET", "/fx/conversions", fixture("conversion_list.json"))
        found = adapter(srv).find_conversions("req-doc-1")
        assert [c.conversion_id for c in found] == ["cnv-doc-0001"]
        assert [r.query.get("page_num") for r in srv.sent("GET", "/fx/conversions")] == ["0", "1"]


def test_simulate_deposit_resolves_the_global_account_first():
    with ReplayServer() as srv:
        srv.on("GET", "/global_accounts", fixture("global_accounts.json"))
        srv.on("POST", "/simulation/deposit/create", fixture("deposit.json"), 201)
        d = adapter(srv).simulate_deposit(request_id="dep-1", currency="USD", amount=D("1000"))
        assert d.deposit_id == "dep-doc-0001" and d.request_id == "dep-1"
        body = srv.sent("POST", "/simulation/deposit/create")[0].body
        assert body["global_account_id"] == "ga-doc-0001" and "currency" not in body


def test_simulate_deposit_with_no_usable_global_account_sends_nothing():
    with ReplayServer() as srv:
        srv.on("GET", "/global_accounts", '{"items": [], "page_after": null}')
        with pytest.raises(AirwallexError) as e:
            adapter(srv).simulate_deposit(request_id="dep-1", currency="USD", amount=D("1000"))
        assert e.value.code == "no_global_account"
        assert not srv.sent("POST", "/simulation/deposit/create")


def test_simulate_transition_sends_next_status():
    with ReplayServer() as srv:
        srv.on("POST", "/simulation/transfers/trf-doc-0001/transition", fixture("transfer_create.json").replace("PROCESSING", "SENT"))
        t = adapter(srv).simulate_transfer_transition("trf-doc-0001", "SENT")
        assert t.status == "SENT"
        assert srv.sent("POST", "/simulation/transfers/trf-doc-0001/transition")[0].body == {"next_status": "SENT"}


def test_get_balances_through_the_adapter_ignores_yield_and_credit():
    with ReplayServer() as srv:
        srv.on("GET", "/balances/current", fixture("balances.json"))
        assert {b.currency: b.available for b in adapter(srv).get_balances()}["USD"] == D("3000")


def test_minted_request_ids_fit_the_64_character_limit():
    from helpers import World
    w = World()
    env = w.prepare("O2").execution.envelope
    for rid in (env.conversion_request_id, env.transfer_request_id):
        assert rid and len(rid) <= 64


def test_create_beneficiary_names_the_type_and_transfer_methods():
    with ReplayServer() as srv:
        srv.on("POST", "/beneficiaries/create", fixture("beneficiary_create.json"), 201)
        adapter(srv).create_beneficiary(request_id="r-ben", payload={"company_name": "Acme Co.", "bank_details": {}})
        body = srv.sent("POST", "/beneficiaries/create")[0].body
        assert body["beneficiary"]["type"] == "BANK_ACCOUNT" and body["transfer_methods"] == ["LOCAL"]
        assert body["request_id"] == "r-ben"

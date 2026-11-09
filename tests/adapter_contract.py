"""Adapter contract: any AirwallexAdapter implementation must pass these tests.

Subclass AdapterContract and implement `make()` returning (adapter, control) where `control` is the
FakeAirwallex that backs the adapter (it exposes advance(), set_rate(), inject()). The HTTP adapter
subclasses this against the local fake server first, and later runs a read-only subset against the sandbox.
"""
from datetime import timedelta
from decimal import Decimal as D

import pytest

from rate_locked.awx.adapter import AirwallexError, UncertainOutcome

BEN = {"name": "Acme Co.", "bank_country_code": "DE", "account_currency": "EUR"}


class AdapterContract:
    def make(self):  # pragma: no cover - overridden
        raise NotImplementedError

    @pytest.fixture
    def env(self):
        adapter, control = self.make()
        yield adapter, control
        close = getattr(control, "close", None)
        if close:
            close()

    # ---- reads -----------------------------------------------------------------------------
    def test_balances(self, env):
        a, _ = env
        bal = {b.currency: b.available for b in a.get_balances()}
        assert bal["USD"] == D("3000.00") and bal["EUR"] == D("1500.00")

    def test_current_rate_is_consistent(self, env):
        a, _ = env
        r = a.get_current_rate(sell_currency="USD", buy_currency="GBP", buy_amount=D("1000"))
        assert r.buy_amount == D("1000.00") and r.sell_amount == D("1300.00")
        assert r.currency_pair == "GBPUSD" and r.rate == D("1.30")

    # ---- quotes ------------------------------------------------------------------------------
    def test_quote_terms_and_retrieval(self, env):
        a, _ = env
        q = a.create_quote(buy_currency="GBP", sell_currency="USD", validity="MIN_1", buy_amount=D("1000"))
        assert q.usage == "SINGLE_USE" and q.sell_amount == D("1300.00") and q.client_rate == D("1.30")
        assert q.valid_to_at - q.valid_from_at == timedelta(seconds=60)
        assert a.get_quote(q.quote_id) == q

    def test_quote_needs_exactly_one_amount(self, env):
        a, _ = env
        with pytest.raises(AirwallexError) as e:
            a.create_quote(buy_currency="GBP", sell_currency="USD", validity="MIN_1")
        assert e.value.code == "validation_error"
        with pytest.raises(AirwallexError):
            a.create_quote(buy_currency="GBP", sell_currency="USD", validity="MIN_1",
                           buy_amount=D("1"), sell_amount=D("1"))

    def test_quote_sell_amount_only(self, env):
        a, _ = env
        q = a.create_quote(buy_currency="GBP", sell_currency="USD", validity="MIN_15", sell_amount=D("1300"))
        assert q.buy_amount == D("1000.00")
        assert q.valid_to_at - q.valid_from_at == timedelta(minutes=15)

    def test_invalid_validity_rejected(self, env):
        a, _ = env
        with pytest.raises(AirwallexError) as e:
            a.create_quote(buy_currency="GBP", sell_currency="USD", validity="MIN_2", buy_amount=D("1"))
        assert e.value.code == "validation_error"

    # ---- conversions ---------------------------------------------------------------------------
    def _convert(self, a, rid="req-c1", buy="1000"):
        q = a.create_quote(buy_currency="GBP", sell_currency="USD", validity="MIN_1", buy_amount=D(buy))
        c = a.create_conversion(request_id=rid, quote_id=q.quote_id, buy_currency="GBP", sell_currency="USD",
                                buy_amount=D(buy), reason="supplier invoice")
        return q, c

    def test_conversion_executes_at_the_quoted_rate_and_moves_balances(self, env):
        a, _ = env
        q, c = self._convert(a)
        assert c.client_rate == q.client_rate and c.quote_id == q.quote_id and c.status == "SETTLED"
        assert c.sell_amount == q.sell_amount and c.buy_amount == q.buy_amount
        bal = {b.currency: b.available for b in a.get_balances()}
        assert bal["USD"] == D("1700.00") and bal["GBP"] == D("1000.00")

    def test_quote_is_single_use(self, env):
        a, _ = env
        q, _c = self._convert(a)
        with pytest.raises(AirwallexError) as e:
            a.create_conversion(request_id="req-c2", quote_id=q.quote_id, buy_currency="GBP",
                                sell_currency="USD", buy_amount=D("1000"))
        assert e.value.code == "invalid_quote_id"

    def test_expired_quote_is_rejected_without_effect(self, env):
        a, ctl = env
        q = a.create_quote(buy_currency="GBP", sell_currency="USD", validity="MIN_1", buy_amount=D("1000"))
        ctl.advance(61)
        with pytest.raises(AirwallexError) as e:
            a.create_conversion(request_id="req-x", quote_id=q.quote_id, buy_currency="GBP",
                                sell_currency="USD", buy_amount=D("1000"))
        assert e.value.code == "expired_quote"
        assert {b.currency: b.available for b in a.get_balances()}["USD"] == D("3000.00")
        assert a.find_conversions("req-x") == []

    def test_quote_valid_to_is_inclusive(self, env):
        a, ctl = env
        q = a.create_quote(buy_currency="GBP", sell_currency="USD", validity="MIN_1", buy_amount=D("10"))
        ctl.advance(60)  # exactly valid_to_at
        c = a.create_conversion(request_id="req-edge", quote_id=q.quote_id, buy_currency="GBP",
                                sell_currency="USD", buy_amount=D("10"))
        assert c.status == "SETTLED"

    def test_unknown_quote(self, env):
        a, _ = env
        with pytest.raises(AirwallexError) as e:
            a.create_conversion(request_id="r", quote_id="nope", buy_currency="GBP", sell_currency="USD",
                                buy_amount=D("1"))
        assert e.value.code == "invalid_quote_id"

    def test_duplicate_request_id_is_rejected_with_no_second_effect(self, env):
        a, _ = env
        self._convert(a, rid="dup")
        q2 = a.create_quote(buy_currency="GBP", sell_currency="USD", validity="MIN_1", buy_amount=D("1000"))
        with pytest.raises(AirwallexError) as e:
            a.create_conversion(request_id="dup", quote_id=q2.quote_id, buy_currency="GBP",
                                sell_currency="USD", buy_amount=D("1000"))
        assert e.value.code == "request_id_duplicate"
        assert {b.currency: b.available for b in a.get_balances()}["USD"] == D("1700.00")

    def test_insufficient_funds(self, env):
        a, _ = env
        q = a.create_quote(buy_currency="GBP", sell_currency="USD", validity="MIN_1", buy_amount=D("5000"))
        with pytest.raises(AirwallexError) as e:
            a.create_conversion(request_id="big", quote_id=q.quote_id, buy_currency="GBP",
                                sell_currency="USD", buy_amount=D("5000"))
        assert e.value.code == "insufficient_funds"

    def test_find_conversion_by_request_id_and_get(self, env):
        a, _ = env
        _q, c = self._convert(a, rid="find-me")
        found = a.find_conversions("find-me")
        assert [x.conversion_id for x in found] == [c.conversion_id]
        assert a.get_conversion(c.conversion_id).request_id == "find-me"
        assert a.find_conversions("unknown") == []

    def test_scheduled_conversion_settles_later(self, env):
        a, ctl = env
        ctl.settle_after_polls = 1
        _q, c = self._convert(a, rid="slow")
        assert c.status == "SCHEDULED"
        assert a.get_conversion(c.conversion_id).status == "SETTLED"

    def test_lookup_can_lag_so_empty_is_not_proof(self, env):
        a, ctl = env
        ctl.lookup_lag = 2
        self._convert(a, rid="lag")
        assert a.find_conversions("lag") == []
        assert a.find_conversions("lag") == []
        assert len(a.find_conversions("lag")) == 1

    # ---- uncertain outcomes ------------------------------------------------------------------
    def test_lost_response_after_effect_raises_uncertain_and_is_found_by_lookup(self, env):
        a, ctl = env
        q = a.create_quote(buy_currency="GBP", sell_currency="USD", validity="MIN_1", buy_amount=D("1000"))
        ctl.inject("create_conversion", "drop_response")
        with pytest.raises(UncertainOutcome) as e:
            a.create_conversion(request_id="lost", quote_id=q.quote_id, buy_currency="GBP",
                                sell_currency="USD", buy_amount=D("1000"))
        assert e.value.request_id == "lost"
        assert len(a.find_conversions("lost")) == 1

    def test_lost_request_before_effect_raises_uncertain_and_lookup_is_empty(self, env):
        a, ctl = env
        q = a.create_quote(buy_currency="GBP", sell_currency="USD", validity="MIN_1", buy_amount=D("1000"))
        ctl.inject("create_conversion", "drop_request")
        with pytest.raises(UncertainOutcome):
            a.create_conversion(request_id="lost2", quote_id=q.quote_id, buy_currency="GBP",
                                sell_currency="USD", buy_amount=D("1000"))
        assert a.find_conversions("lost2") == []

    # ---- beneficiaries and transfers --------------------------------------------------------------
    def _beneficiary(self, a):
        return a.create_beneficiary(request_id="ben-req", payload=BEN)

    def test_beneficiary_schema_has_required_fields(self, env):
        a, _ = env
        s = a.get_beneficiary_schema(country_code="GB", currency="GBP", entity_type="COMPANY",
                                     transfer_method="LOCAL")
        assert isinstance(s, dict) and s

    def test_transfer_lifecycle_and_lookup(self, env):
        a, _ = env
        ben = self._beneficiary(a)
        t = a.create_transfer(request_id="trf-1", beneficiary_id=ben.beneficiary_id, source_currency="EUR",
                              transfer_currency="EUR", transfer_amount=D("1200"), reason="supplier invoice",
                              reference="INV-1001")
        assert t.status == "PROCESSING" and t.fee_amount == D("0")  # never shown as PAID yet
        assert [x.transfer_id for x in a.find_transfers("trf-1")] == [t.transfer_id]
        assert {b.currency: b.available for b in a.get_balances()}["EUR"] == D("300.00")
        paid = a.simulate_transfer_transition(t.transfer_id, "PAID")
        assert paid.status == "PAID" and a.get_transfer(t.transfer_id).status == "PAID"

    def test_failed_transfer_refunds(self, env):
        a, _ = env
        ben = self._beneficiary(a)
        t = a.create_transfer(request_id="trf-2", beneficiary_id=ben.beneficiary_id, source_currency="EUR",
                              transfer_currency="EUR", transfer_amount=D("1200"), reason="r", reference="x")
        a.simulate_transfer_transition(t.transfer_id, "FAILED")
        assert {b.currency: b.available for b in a.get_balances()}["EUR"] == D("1500.00")

    def test_swift_transfer_charges_the_flat_fee(self, env):
        a, _ = env
        ben = self._beneficiary(a)
        t = a.create_transfer(request_id="trf-3", beneficiary_id=ben.beneficiary_id, source_currency="EUR",
                              transfer_currency="EUR", transfer_amount=D("100"), reason="r", reference="x",
                              transfer_method="SWIFT")
        assert t.fee_amount == D("12.85")
        assert {b.currency: b.available for b in a.get_balances()}["EUR"] == D("1387.15")

    def test_duplicate_transfer_request_is_rejected_with_no_second_debit(self, env):
        a, _ = env
        ben = self._beneficiary(a)
        kw = dict(request_id="trf-d", beneficiary_id=ben.beneficiary_id, source_currency="EUR",
                  transfer_currency="EUR", transfer_amount=D("100"), reason="r", reference="x")
        a.create_transfer(**kw)
        with pytest.raises(AirwallexError) as e:
            a.create_transfer(**kw)
        assert e.value.code == "request_id_duplicate"
        assert {b.currency: b.available for b in a.get_balances()}["EUR"] == D("1400.00")

    def test_transfer_needs_funds_and_a_known_beneficiary(self, env):
        a, _ = env
        ben = self._beneficiary(a)
        with pytest.raises(AirwallexError) as e:
            a.create_transfer(request_id="t-big", beneficiary_id=ben.beneficiary_id, source_currency="EUR",
                              transfer_currency="EUR", transfer_amount=D("99999"), reason="r", reference="x")
        assert e.value.code == "insufficient_funds"
        with pytest.raises(AirwallexError) as e:
            a.create_transfer(request_id="t-nb", beneficiary_id="missing", source_currency="EUR",
                              transfer_currency="EUR", transfer_amount=D("1"), reason="r", reference="x")
        assert e.value.code == "beneficiary_not_found"

    def test_lost_transfer_response_is_found_by_lookup(self, env):
        a, ctl = env
        ben = self._beneficiary(a)
        ctl.inject("create_transfer", "drop_response")
        with pytest.raises(UncertainOutcome):
            a.create_transfer(request_id="t-lost", beneficiary_id=ben.beneficiary_id, source_currency="EUR",
                              transfer_currency="EUR", transfer_amount=D("100"), reason="r", reference="x")
        assert len(a.find_transfers("t-lost")) == 1

    # ---- simulation ------------------------------------------------------------------------------
    def test_simulated_deposit_adds_funds(self, env):
        a, _ = env
        d = a.simulate_deposit(request_id="dep-1", currency="USD", amount=D("1000"))
        assert d.status == "SETTLED"
        assert {b.currency: b.available for b in a.get_balances()}["USD"] == D("4000.00")

"""Local fake of the Airwallex endpoints this product uses. In-process adapter; fake_server.py serves the same
logic over HTTP.

Use it to build and test the real adapter without keys.
It is a model of the PUBLIC reference and the builder guide, not of the live sandbox. Where behavior is
unverified it is marked ASSUMED; the sandbox recordings of 2026-11-05 cover only part of it.

Behaviors modeled (F11, F14, F15 in the spec):
  * Quote terms with validity windows, SINGLE_USE, valid_to_at inclusive.
  * Conversion with a quote executes at the quote's client_rate; expired quote -> expired_quote;
    unknown or used quote -> invalid_quote_id; duplicate request_id is rejected (no second effect).
  * Conversion statuses SCHEDULED -> SETTLED (settle_after_polls), lookup by request_id with optional lag.
  * Transfers: PROCESSING until a simulated transition; FAILED refunds (ASSUMED); flat 12.85 SWIFT fee (guide).
  * Faults for tests: inject(op, "drop_response") applies the effect then raises UncertainOutcome;
    inject(op, "drop_request") raises UncertainOutcome with no effect.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from ..clock import ManualClock
from .adapter import (QUOTE_VALIDITIES, AirwallexError, Balance, Beneficiary, Conversion, Deposit,
                      RateEstimate, QuoteTerms, Transfer, UncertainOutcome)

CENT = Decimal("0.01")
VALIDITY_SECONDS = {"MIN_1": 60, "MIN_15": 900, "MIN_30": 1800, "HR_1": 3600, "HR_4": 14400,
                    "HR_8": 28800, "HR_24": 86400}
SWIFT_FEE = Decimal("12.85")
# Transfer lifecycle from the reference's status page (2026-11-01), for strict simulated transitions.
NEXT_STATUS = {"PROCESSING": ("SENT", "FAILED", "CANCELLED"), "SENT": ("PAID", "FAILED"), "PAID": ("FAILED",),
               "FAILED": ("CANCELLED",)}


def q2(x: Decimal) -> Decimal:
    return x.quantize(CENT, rounding=ROUND_HALF_UP)


class FakeAirwallex:
    def __init__(self, *, balances: dict, pairs: dict, clock=None, settle_after_polls: int = 0,
                 lookup_lag: int = 0, strict_transitions: bool = False):
        self.clock = clock or ManualClock()
        self._balances = {c: Decimal(str(v)) for c, v in balances.items()}
        self._pairs = {p: Decimal(str(r)) for p, r in pairs.items()}
        self.settle_after_polls = settle_after_polls
        self.lookup_lag = lookup_lag
        # The reference says a simulated transition works only if next_status is the NEXT valid status
        # (PROCESSING -> SENT -> PAID; PAID may still fail later). Strict mode enforces that; the default stays
        # lenient so older tests and the console's FAKE controls can jump straight to PAID.
        self.strict_transitions = strict_transitions
        self._faults: dict = {}
        self._seq = 0
        self._quotes: dict = {}
        self._used_quotes: set = set()
        self._conversions: dict = {}
        self._conv_polls: dict = {}
        self._beneficiaries: dict = {}
        self._transfers: dict = {}
        self._request_ids: dict = {}   # (kind, request_id) -> object id
        self._lookups_seen: dict = {}  # (kind, request_id) -> number of find calls
        self._lag: dict = {}           # (kind, request_id) -> lookups to skip

    @classmethod
    def demo(cls, **kw):
        return cls(balances={"USD": "3000.00", "EUR": "1500.00", "GBP": "0.00"},
                   pairs={"EURUSD": "1.08", "GBPUSD": "1.30"}, **kw)

    # ---- test controls ----------------------------------------------------------------------
    def advance(self, seconds: float):
        return self.clock.advance(seconds)

    def set_rate(self, pair: str, rate) -> None:
        self._pairs[pair] = Decimal(str(rate))

    def inject(self, op: str, mode: str, times: int = 1) -> None:
        if mode not in ("drop_response", "drop_request"):
            raise ValueError(mode)
        self._faults[op] = (mode, times)

    def _take_fault(self, op: str) -> Optional[str]:
        if op not in self._faults:
            return None
        mode, times = self._faults[op]
        if times <= 1:
            del self._faults[op]
        else:
            self._faults[op] = (mode, times - 1)
        return mode

    def _guard(self, fault, op, request_id):
        if fault == "drop_request":
            raise UncertainOutcome(op, request_id, "request lost before it reached the provider (injected)")

    def _finish(self, fault, op, request_id, result):
        if fault == "drop_response":
            raise UncertainOutcome(op, request_id, "response lost after the provider acted (injected)")
        return result

    def _next(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}_{self._seq:04d}"

    # ---- helpers ------------------------------------------------------------------------------
    def _pair_for(self, a: str, b: str):
        for p in (a + b, b + a):
            if p in self._pairs:
                return p, self._pairs[p]
        raise AirwallexError("invalid_currency_pair", f"no pair for {a}/{b}")

    def _amounts(self, sell, buy, buy_amount, sell_amount):
        if (buy_amount is None) == (sell_amount is None):
            raise AirwallexError("validation_error", "exactly one of buy_amount or sell_amount is required")
        pair, rate = self._pair_for(sell, buy)
        buy_is_base = pair.startswith(buy)
        if buy_amount is not None:
            buy_amount = q2(Decimal(buy_amount))
            sell_amount = q2(buy_amount * rate) if buy_is_base else q2(buy_amount / rate)
        else:
            sell_amount = q2(Decimal(sell_amount))
            buy_amount = q2(sell_amount / rate) if buy_is_base else q2(sell_amount * rate)
        return pair, rate, sell_amount, buy_amount

    def _bal(self, currency: str) -> Decimal:
        return self._balances.get(currency, Decimal("0"))

    # ---- AirwallexAdapter -------------------------------------------------------------------------
    def get_balances(self):
        return [Balance(c, q2(v)) for c, v in sorted(self._balances.items())]

    def get_current_rate(self, *, sell_currency, buy_currency, buy_amount=None, sell_amount=None):
        pair, rate, sell, buy = self._amounts(sell_currency, buy_currency, buy_amount, sell_amount)
        return RateEstimate(sell_currency, buy_currency, sell, buy, pair, rate, self.clock.now())

    def create_quote(self, *, buy_currency, sell_currency, validity, buy_amount=None, sell_amount=None):
        if validity not in QUOTE_VALIDITIES:
            raise AirwallexError("validation_error", f"validity must be one of {QUOTE_VALIDITIES}")
        if buy_currency == sell_currency:
            raise AirwallexError("validation_error", "buy and sell currency must differ")
        pair, rate, sell, buy = self._amounts(sell_currency, buy_currency, buy_amount, sell_amount)
        now = self.clock.now()
        q = QuoteTerms(self._next("qte"), sell_currency, buy_currency, sell, buy, rate, now,
                       now + timedelta(seconds=VALIDITY_SECONDS[validity]), "SINGLE_USE", pair)
        self._quotes[q.quote_id] = q
        return q

    def get_quote(self, quote_id):
        if quote_id not in self._quotes:
            raise AirwallexError("not_found", f"quote {quote_id}", 404)
        return self._quotes[quote_id]

    def create_conversion(self, *, request_id, quote_id, buy_currency, sell_currency,
                          buy_amount=None, sell_amount=None, reason=""):
        fault = self._take_fault("create_conversion")
        self._guard(fault, "create_conversion", request_id)
        if ("conversion", request_id) in self._request_ids:
            raise AirwallexError("request_id_duplicate", f"request_id {request_id} already used")
        q = self._quotes.get(quote_id)
        if q is None:
            raise AirwallexError("invalid_quote_id", "unknown quote")
        if self.clock.now() > q.valid_to_at:
            raise AirwallexError("expired_quote", "quote validity has passed")
        if quote_id in self._used_quotes:
            raise AirwallexError("invalid_quote_id", "quote already used (single use)")
        if (q.buy_currency, q.sell_currency) != (buy_currency, sell_currency):
            raise AirwallexError("validation_error", "currencies do not match the quote")
        if buy_amount is not None and q2(Decimal(buy_amount)) != q.buy_amount:
            raise AirwallexError("validation_error", "buy_amount does not match the quote")
        if sell_amount is not None and q2(Decimal(sell_amount)) != q.sell_amount:
            raise AirwallexError("validation_error", "sell_amount does not match the quote")
        if self._bal(q.sell_currency) < q.sell_amount:
            raise AirwallexError("insufficient_funds", f"{q.sell_currency} balance too low")
        self._used_quotes.add(quote_id)
        self._balances[q.sell_currency] = self._bal(q.sell_currency) - q.sell_amount
        cid = self._next("cnv")
        polls = self.settle_after_polls
        status = "SETTLED" if polls <= 0 else "SCHEDULED"
        if status == "SETTLED":
            self._balances[q.buy_currency] = self._bal(q.buy_currency) + q.buy_amount
        c = Conversion(cid, request_id, status, q.sell_currency, q.buy_currency, q.sell_amount, q.buy_amount,
                       q.client_rate, quote_id, self.clock.now())
        self._conversions[cid] = c
        self._conv_polls[cid] = polls
        self._request_ids[("conversion", request_id)] = cid
        self._lag[("conversion", request_id)] = self.lookup_lag
        return self._finish(fault, "create_conversion", request_id, c)

    def get_conversion(self, conversion_id):
        c = self._conversions.get(conversion_id)
        if c is None:
            raise AirwallexError("not_found", f"conversion {conversion_id}", 404)
        if c.status == "SCHEDULED":
            self._conv_polls[conversion_id] -= 1
            if self._conv_polls[conversion_id] <= 0:
                self._balances[c.buy_currency] = self._bal(c.buy_currency) + c.buy_amount
                c = Conversion(**{**c.__dict__, "status": "SETTLED"})
                self._conversions[conversion_id] = c
        return c

    def find_conversions(self, request_id):
        key = ("conversion", request_id)
        cid = self._request_ids.get(key)
        if cid is None:
            return []
        seen = self._lookups_seen.get(key, 0)
        self._lookups_seen[key] = seen + 1
        if seen < self._lag.get(key, 0):
            return []  # lookup visibility lag: empty is not proof of absence
        return [self._conversions[cid]]

    def get_beneficiary_schema(self, *, country_code, currency, entity_type, transfer_method):
        return {"country_code": country_code, "currency": currency, "entity_type": entity_type,
                "transfer_method": transfer_method,
                "required": ["name", "bank_country_code", "account_currency"], "assumed": True}

    def create_beneficiary(self, *, request_id, payload):
        if ("beneficiary", request_id) in self._request_ids:
            raise AirwallexError("request_id_duplicate", f"request_id {request_id} already used")
        bid = self._next("ben")
        b = Beneficiary(bid, request_id, {"name": payload.get("name", ""), "bank_country_code": payload.get("bank_country_code", "")})
        self._beneficiaries[bid] = b
        self._request_ids[("beneficiary", request_id)] = bid
        return b

    def create_transfer(self, *, request_id, beneficiary_id, source_currency, transfer_currency,
                        transfer_amount, reason, reference, transfer_method="LOCAL"):
        fault = self._take_fault("create_transfer")
        self._guard(fault, "create_transfer", request_id)
        if ("transfer", request_id) in self._request_ids:
            raise AirwallexError("request_id_duplicate", f"request_id {request_id} already used")
        if beneficiary_id not in self._beneficiaries:
            raise AirwallexError("beneficiary_not_found", beneficiary_id)
        if source_currency != transfer_currency:
            raise AirwallexError("validation_error", "this fake supports same-currency payouts only")
        amount = q2(Decimal(transfer_amount))
        fee = SWIFT_FEE if transfer_method == "SWIFT" else Decimal("0")
        if self._bal(source_currency) < amount + fee:
            raise AirwallexError("insufficient_funds", f"{source_currency} balance too low")
        self._balances[source_currency] = self._bal(source_currency) - amount - fee
        tid = self._next("trf")
        t = Transfer(tid, request_id, "PROCESSING", beneficiary_id, source_currency, transfer_currency,
                     amount, fee, self.clock.now())
        self._transfers[tid] = t
        self._request_ids[("transfer", request_id)] = tid
        self._lag[("transfer", request_id)] = self.lookup_lag
        return self._finish(fault, "create_transfer", request_id, t)

    def validate_transfer(self, *, beneficiary_id, source_currency, transfer_amount):
        """POST /transfers/validate: no effect. Not part of the adapter protocol."""
        if beneficiary_id not in self._beneficiaries:
            raise AirwallexError("beneficiary_not_found", beneficiary_id)
        if self._bal(source_currency) < q2(Decimal(transfer_amount)):
            raise AirwallexError("insufficient_funds", f"{source_currency} balance too low")

    def get_transfer(self, transfer_id):
        t = self._transfers.get(transfer_id)
        if t is None:
            raise AirwallexError("not_found", f"transfer {transfer_id}", 404)
        return t

    def find_transfers(self, request_id, *, beneficiary_id=None):
        key = ("transfer", request_id)
        tid = self._request_ids.get(key)
        if tid is None:
            return []
        seen = self._lookups_seen.get(key, 0)
        self._lookups_seen[key] = seen + 1
        if seen < self._lag.get(key, 0):
            return []
        t = self._transfers[tid]
        return [t] if beneficiary_id in (None, t.beneficiary_id) else []

    def simulate_deposit(self, *, request_id, currency, amount):
        if ("deposit", request_id) in self._request_ids:
            raise AirwallexError("request_id_duplicate", f"request_id {request_id} already used")
        amount = q2(Decimal(amount))
        self._balances[currency] = self._bal(currency) + amount
        did = self._next("dep")
        self._request_ids[("deposit", request_id)] = did
        return Deposit(did, request_id, currency, amount, "SETTLED")

    def simulate_transfer_transition(self, transfer_id, transition):
        t = self.get_transfer(transfer_id)
        if transition not in ("SENT", "PAID", "FAILED", "CANCELLED", "PROCESSING"):
            raise AirwallexError("validation_error", f"unsupported transition {transition}")
        if self.strict_transitions:
            if transition not in NEXT_STATUS.get(t.status, ()):
                raise AirwallexError("invalid_transition", f"{t.status} cannot move to {transition}")
        elif t.status in ("FAILED", "CANCELLED") or (t.status == "PAID" and transition != "FAILED"):
            raise AirwallexError("invalid_transition", f"transfer already {t.status}")  # PAID may still fail late
        if transition in ("FAILED", "CANCELLED"):  # ASSUMED: provider returns the funds
            self._balances[t.source_currency] = self._bal(t.source_currency) + t.transfer_amount + t.fee_amount
        t = Transfer(**{**t.__dict__, "status": transition})
        self._transfers[transfer_id] = t
        return t

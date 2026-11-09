"""Airwallex adapter boundary: the interface, plain data types and errors.

This file is the contract between the adapter slice (rate_locked.awx)
and the rest of the product (planner, approval binding, state machine, record).
The core imports ONLY this module from rate_locked.awx (a test enforces it).

Rules the implementation must keep:
  * Money is Decimal. Amounts cross the wire as strings or numbers, never as float math.
  * Create calls (quote, conversion, beneficiary, transfer, deposit) are NEVER retried.
    When the outcome of a create call is unknown (timeout, dropped connection, 5xx after send)
    raise UncertainOutcome. The caller looks the operation up by request_id.
  * The caller mints every request_id before the call; the adapter does not invent them.
  * Never send x-api-version on FX calls (builder guide, Kit 1 notes).
  * Sandbox host only: https://api.sandbox.airwallex.com (plus loopback for the local fake).
  * Response shapes in wire.py follow the public reference; the 2026-11-05 sandbox recordings confirm the fields they contain.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Optional, Protocol, runtime_checkable

QUOTE_VALIDITIES = ("MIN_1", "MIN_15", "MIN_30", "HR_1", "HR_4", "HR_8", "HR_24")

# Conversion statuses from the public reference (F15). Anything else is kept verbatim.
CONVERSION_STATUSES = ("SCHEDULED", "CANCELLED", "OVERDUE", "SETTLED")
# Transfer statuses: the probe recorded SCHEDULED, SENT and PAID; unknown values must pass through verbatim.
TRANSFER_PAID = "PAID"
TRANSFER_FAILED = ("FAILED", "CANCELLED", "RETURNED")


class AirwallexError(Exception):
    """The provider answered with an error. The request was NOT accepted (no financial effect)."""

    def __init__(self, code: str, message: str = "", http_status: int = 400):
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.message = message
        self.http_status = http_status


# Codes the reference documents for a create call whose request_id was already seen: 400 request_id_duplicate (the
# request exists) and 409 request_pending (it is still being processed). Either way the money may be moving, so the
# right reaction is a lookup by request_id, never "refused". `duplicate_request` is kept for older fakes.
LOOKUP_CODES = ("request_id_duplicate", "request_pending", "duplicate_request")


def means_lookup(err: "AirwallexError") -> bool:
    """True when a provider error on a create call means 'look the request up', not 'the request failed'."""
    return err.http_status == 409 or err.code in LOOKUP_CODES


class UncertainOutcome(Exception):
    """The request may or may not have taken effect. Look it up by request_id; never re-send."""

    def __init__(self, operation: str, request_id: str, detail: str = ""):
        super().__init__(f"uncertain outcome for {operation} request_id={request_id} {detail}".strip())
        self.operation = operation
        self.request_id = request_id
        self.detail = detail


@dataclass(frozen=True)
class Balance:
    currency: str
    available: Decimal


@dataclass(frozen=True)
class RateEstimate:
    """Indicative rate from GET /fx/rates/current. Not a quote: never bind an approval to it."""

    sell_currency: str
    buy_currency: str
    sell_amount: Decimal
    buy_amount: Decimal
    currency_pair: str
    rate: Decimal
    created_at: datetime


@dataclass(frozen=True)
class QuoteTerms:
    """A locked quote. The approval binds to every field here."""

    quote_id: str
    sell_currency: str
    buy_currency: str
    sell_amount: Decimal
    buy_amount: Decimal
    client_rate: Decimal
    valid_from_at: datetime
    valid_to_at: datetime  # inclusive
    usage: str = "SINGLE_USE"
    currency_pair: str = ""


@dataclass(frozen=True)
class Conversion:
    conversion_id: str
    request_id: str
    status: str  # verbatim provider status
    sell_currency: str
    buy_currency: str
    sell_amount: Decimal
    buy_amount: Decimal
    client_rate: Decimal
    quote_id: Optional[str] = None
    created_at: Optional[datetime] = None


@dataclass(frozen=True)
class Beneficiary:
    beneficiary_id: str
    request_id: str
    summary: dict = field(default_factory=dict)  # sanitized, never full account numbers


@dataclass(frozen=True)
class Transfer:
    transfer_id: str
    request_id: str
    status: str  # verbatim provider status; PROCESSING is never shown as PAID
    beneficiary_id: str
    source_currency: str
    transfer_currency: str
    transfer_amount: Decimal
    fee_amount: Decimal = Decimal("0")
    created_at: Optional[datetime] = None
    beneficiary_from: str = "reply"  # "request" when the provider's reply named no beneficiary and our own request's id is used


@dataclass(frozen=True)
class Deposit:
    deposit_id: str
    request_id: str
    currency: str
    amount: Decimal
    status: str = "SETTLED"


@runtime_checkable
class AirwallexAdapter(Protocol):
    """Everything the product needs from Airwallex. One method per endpoint we call."""

    # GET /api/v1/balances/current
    def get_balances(self) -> list[Balance]: ...

    # GET /api/v1/fx/rates/current
    def get_current_rate(
        self, *, sell_currency: str, buy_currency: str,
        buy_amount: Optional[Decimal] = None, sell_amount: Optional[Decimal] = None,
    ) -> RateEstimate: ...

    # POST /api/v1/fx/quotes/create   (exactly one of buy_amount / sell_amount)
    def create_quote(
        self, *, buy_currency: str, sell_currency: str, validity: str,
        buy_amount: Optional[Decimal] = None, sell_amount: Optional[Decimal] = None,
    ) -> QuoteTerms: ...

    # GET /api/v1/fx/quotes/{quote_id}
    def get_quote(self, quote_id: str) -> QuoteTerms: ...

    # POST /api/v1/fx/conversions/create   (quote_id is required by this product; never convert at spot)
    def create_conversion(
        self, *, request_id: str, quote_id: str, buy_currency: str, sell_currency: str,
        buy_amount: Optional[Decimal] = None, sell_amount: Optional[Decimal] = None,
        reason: str = "",
    ) -> Conversion: ...

    # GET /api/v1/fx/conversions/{conversion_id}
    def get_conversion(self, conversion_id: str) -> Conversion: ...

    # GET /api/v1/fx/conversions?request_id=...   (empty list is NOT proof of absence: lookup can lag)
    def find_conversions(self, request_id: str) -> list[Conversion]: ...

    # POST /api/v1/beneficiaries/schema  (guide path) or /beneficiary_api_schemas/generate (reference path)
    def get_beneficiary_schema(
        self, *, country_code: str, currency: str, entity_type: str, transfer_method: str,
    ) -> dict: ...

    # POST /api/v1/beneficiaries/create
    def create_beneficiary(self, *, request_id: str, payload: dict) -> Beneficiary: ...

    # POST /api/v1/transfers/create
    def create_transfer(
        self, *, request_id: str, beneficiary_id: str, source_currency: str,
        transfer_currency: str, transfer_amount: Decimal, reason: str, reference: str,
        transfer_method: str = "LOCAL",
    ) -> Transfer: ...

    # GET /api/v1/transfers/{transfer_id}
    def get_transfer(self, transfer_id: str) -> Transfer: ...

    # GET /api/v1/transfers?request_id=...   (beneficiary_id narrows the list and, because the reference's transfer
    # object may not echo it, is what the adapter attaches to records it returns)
    def find_transfers(self, request_id: str, *, beneficiary_id: Optional[str] = None) -> list[Transfer]: ...

    # POST /api/v1/simulation/deposit/create   (sandbox only: adds test funds; the HTTP adapter finds the global account)
    def simulate_deposit(self, *, request_id: str, currency: str, amount: Decimal) -> Deposit: ...

    # POST /api/v1/simulation/transfers/{transfer_id}/transition   (sandbox only; body field next_status)
    def simulate_transfer_transition(self, transfer_id: str, transition: str) -> Transfer: ...

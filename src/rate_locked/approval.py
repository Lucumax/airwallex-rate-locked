"""Approval binding: a person approves the exact terms; nothing else is authority.

An Envelope carries every term the approver saw: supplier, invoice, payout amount, the locked quote (id, rate,
amounts, validity), pre-minted request IDs and the plan it came from. Its digest is a SHA-256 over a canonical
form. The approver page presents that digest back; any mismatch voids the token.

The gate issues single-use tokens (stored as hashes), expires approvals at
    min(quote.valid_to_at - dispatch_margin, created_at + ttl)
and allows exactly one dispatch authorization per approved envelope. The forecast-confidence ceiling in the
planner can send a conversion to a person earlier, but it never removes this approval.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import threading
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Callable, Optional

from .awx.adapter import QuoteTerms
from .models import Action, Obligation
from .planner import Decision

DEFAULT_TTL = timedelta(minutes=10)
DEFAULT_MARGIN = timedelta(seconds=10)
WORLD = "AIRWALLEX_SANDBOX"

MESSAGES = {
    "EXPIRED": "The approval window closed. Nothing was converted.",
    "TOO_CLOSE_TO_EXPIRY": "Too close to expiry to execute. Ask for a new quote.",
    "NONCE_USED": "This approval link was already used.",
    "TERMS_CHANGED": "These terms changed after the request. Ask again.",
    "UNKNOWN_TOKEN": "This approval link is not recognised.",
    "NOT_APPROVED": "There is no approval for this request.",
    "ALREADY_DISPATCHED": "This approval was already used to execute.",
    "DENIED": "Denied. Nothing was converted.",
    "NOT_YET_VALID": "This quote is not valid yet. Ask for a new quote.",
}


class EnvelopeError(Exception):
    def __init__(self, code: str, message: str = ""):
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code


class ApprovalError(Exception):
    def __init__(self, code: str, message: str = ""):
        self.code = code
        self.message = message or MESSAGES.get(code, code)
        super().__init__(f"{code}: {self.message}")


def _s(x) -> Optional[str]:
    return None if x is None else (format(x, "f") if isinstance(x, Decimal) else str(x))


def _ts(dt: datetime) -> str:
    return dt.isoformat()


def canonical_digest(canonical: dict) -> str:
    raw = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


@dataclass(frozen=True)
class Envelope:
    envelope_id: str
    plan_digest: str
    obligation_id: str
    supplier_name: str
    beneficiary_id: str
    invoice_ref: str
    mode: Action
    payout_currency: str
    payout_method: str
    payout_amount: Decimal
    payout_fee: Decimal
    existing_balance_used: Decimal
    quote: Optional[QuoteTerms]
    conversion_request_id: Optional[str]
    transfer_request_id: str
    created_at: datetime
    expires_at: datetime
    world: str = WORLD

    def canonical(self) -> dict:
        q = self.quote
        return {
            "envelope_id": self.envelope_id, "plan_digest": self.plan_digest,
            "obligation_id": self.obligation_id, "supplier_name": self.supplier_name,
            "beneficiary_id": self.beneficiary_id, "invoice_ref": self.invoice_ref, "mode": self.mode.value,
            "payout_currency": self.payout_currency, "payout_method": self.payout_method,
            "payout_amount": _s(self.payout_amount),
            "payout_fee": _s(self.payout_fee), "existing_balance_used": _s(self.existing_balance_used),
            "quote": None if q is None else {
                "quote_id": q.quote_id, "sell_currency": q.sell_currency, "buy_currency": q.buy_currency,
                "sell_amount": _s(q.sell_amount), "buy_amount": _s(q.buy_amount),
                "client_rate": _s(q.client_rate), "valid_from_at": _ts(q.valid_from_at),
                "valid_to_at": _ts(q.valid_to_at), "usage": q.usage},
            "conversion_request_id": self.conversion_request_id,
            "transfer_request_id": self.transfer_request_id,
            "created_at": _ts(self.created_at), "expires_at": _ts(self.expires_at), "world": self.world,
        }

    @property
    def digest(self) -> str:
        return canonical_digest(self.canonical())


def build_envelope(decision: Decision, obligation: Obligation, quote: Optional[QuoteTerms], *,
                   now: datetime, plan_digest: str, ttl: timedelta = DEFAULT_TTL,
                   dispatch_margin: timedelta = DEFAULT_MARGIN,
                   ids: Callable[[], str] = lambda: uuid.uuid4().hex) -> Envelope:
    if decision.action not in (Action.FUND, Action.CONVERT):
        raise EnvelopeError("NOT_EXECUTABLE", f"{decision.action.value} decisions are never executed by the agent")
    if decision.obligation_id != obligation.obligation_id:
        raise EnvelopeError("DECISION_MISMATCH", "decision belongs to another obligation")
    need = obligation.amount + obligation.payout_fee
    if decision.action is Action.FUND:
        if quote is not None:
            raise EnvelopeError("QUOTE_MISMATCH", "a FUND decision carries no quote")
        if decision.from_balance < need:
            raise EnvelopeError("UNDERFUNDED", "existing balance does not cover the payout")
        expires = now + ttl
        conv_req = None
    else:
        if quote is None:
            raise EnvelopeError("QUOTE_MISMATCH", "a CONVERT decision needs a locked quote")
        if (quote.buy_currency != obligation.currency or quote.sell_currency != decision.sell_currency
                or quote.buy_amount != decision.convert_buy_amount):
            raise EnvelopeError("QUOTE_MISMATCH", "the quote does not match the planned conversion")
        if decision.from_balance + quote.buy_amount < need:
            raise EnvelopeError("UNDERFUNDED", "the conversion would not cover the payout and fee")
        expires = min(quote.valid_to_at - dispatch_margin, now + ttl)
        conv_req = ids()
    if expires <= now:
        raise EnvelopeError("TOO_CLOSE_TO_EXPIRY", MESSAGES["TOO_CLOSE_TO_EXPIRY"])
    return Envelope(
        envelope_id=ids(), plan_digest=plan_digest, obligation_id=obligation.obligation_id,
        supplier_name=obligation.supplier_name, beneficiary_id=obligation.beneficiary_id,
        invoice_ref=obligation.invoice_ref, mode=decision.action, payout_currency=obligation.currency,
        payout_method=obligation.payout_method, payout_amount=obligation.amount, payout_fee=obligation.payout_fee,
        existing_balance_used=decision.from_balance, quote=quote, conversion_request_id=conv_req,
        transfer_request_id=ids(), created_at=now, expires_at=expires)


@dataclass(frozen=True)
class Approval:
    envelope_id: str
    approver_id: str
    approved_at: datetime
    envelope_digest: str


class _Slot:
    __slots__ = ("envelope", "state", "approval", "dispatched")

    def __init__(self, envelope: Envelope):
        self.envelope = envelope
        self.state = "ISSUED"  # ISSUED -> USED | DENIED | VOIDED | EXPIRED
        self.approval: Optional[Approval] = None
        self.dispatched = False


def _quote_core(q: QuoteTerms) -> tuple:
    """The fields that decide money and validity. Formatting-only fields (usage, currency pair text) are ignored."""
    return (q.quote_id, q.sell_currency, q.buy_currency, q.sell_amount, q.buy_amount, q.client_rate,
            q.valid_from_at, q.valid_to_at)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class ApprovalGate:
    """Server-side approval state. The approver page only ever holds the token and the digest it displayed."""

    def __init__(self, clock, *, dispatch_margin: timedelta = DEFAULT_MARGIN,
                 token_factory: Callable[[], str] = lambda: secrets.token_urlsafe(32)):
        self._clock = clock
        self.dispatch_margin = dispatch_margin
        self._token_factory = token_factory
        self._by_hash: dict = {}
        self._by_envelope: dict = {}
        self._lock = threading.RLock()

    def issue(self, envelope: Envelope) -> str:
        with self._lock:
            if envelope.envelope_id in self._by_envelope:
                raise ValueError("this envelope was already issued; ask again with a new envelope")
            token = self._token_factory()
            slot = _Slot(envelope)
            self._by_hash[_hash(token)] = slot
            self._by_envelope[envelope.envelope_id] = slot
            return token

    def _slot(self, token: str) -> _Slot:
        slot = self._by_hash.get(_hash(token))
        if slot is None:
            raise ApprovalError("UNKNOWN_TOKEN")
        if slot.state != "ISSUED":
            raise ApprovalError("NONCE_USED")
        return slot

    def approve(self, token: str, presented_digest: str, approver_id: str) -> Approval:
        with self._lock:
            return self._approve(token, presented_digest, approver_id)

    def _approve(self, token: str, presented_digest: str, approver_id: str) -> Approval:
        slot = self._slot(token)
        env = slot.envelope
        if presented_digest != env.digest:
            slot.state = "VOIDED"  # the approver saw different terms: burn the token, ask again
            raise ApprovalError("TERMS_CHANGED")
        if self._clock.now() >= env.expires_at:
            slot.state = "EXPIRED"
            raise ApprovalError("EXPIRED")
        slot.state = "USED"
        slot.approval = Approval(env.envelope_id, approver_id, self._clock.now(), env.digest)
        return slot.approval

    def peek(self, token: str) -> Envelope:
        """The envelope behind a token, without using it. Raises UNKNOWN_TOKEN."""
        slot = self._by_hash.get(_hash(token))
        if slot is None:
            raise ApprovalError("UNKNOWN_TOKEN")
        return slot.envelope

    def deny(self, token: str, approver_id: str) -> None:
        with self._lock:
            slot = self._slot(token)
            slot.state = "DENIED"

    def bound_envelope(self, envelope_id: str) -> Envelope:
        """The immutable envelope the approver saw. Execution must use this, nothing else."""
        slot = self._by_envelope.get(envelope_id)
        if slot is None:
            raise ApprovalError("NOT_APPROVED")
        return slot.envelope

    def bound(self, envelope_id: str):
        """(envelope, approval) as the gate holds them. Raises NOT_APPROVED if there is no approval."""
        slot = self._by_envelope.get(envelope_id)
        if slot is None or slot.approval is None:
            raise ApprovalError("NOT_APPROVED")
        return slot.envelope, slot.approval

    def void(self, envelope_id: str) -> None:
        """Spend the approval without executing (the request failed before dispatch)."""
        with self._lock:
            slot = self._by_envelope.get(envelope_id)
            if slot is not None:
                slot.dispatched = True

    def recheck(self, envelope_id: str) -> None:
        """Cheap deadline check, called immediately before a financial send. Does not consume anything."""
        slot = self._by_envelope.get(envelope_id)
        if slot is None or slot.approval is None:
            raise ApprovalError("NOT_APPROVED")
        self._deadline(slot.envelope)

    def _deadline(self, env: Envelope) -> None:
        now = self._clock.now()
        if now >= env.expires_at:
            raise ApprovalError("EXPIRED")
        if env.quote is not None:
            if now < env.quote.valid_from_at:
                raise ApprovalError("NOT_YET_VALID")
            if now > env.quote.valid_to_at - self.dispatch_margin:
                raise ApprovalError("TOO_CLOSE_TO_EXPIRY")

    def authorize(self, envelope_id: str, live_quote: Optional[QuoteTerms], presented_digest: str) -> Approval:
        """Called once, immediately before the first financial call for this envelope.

        `presented_digest` is the digest of the envelope the executor is about to execute; it must equal the
        digest that was approved."""
        with self._lock:
            slot = self._by_envelope.get(envelope_id)
            if slot is None or slot.approval is None:
                raise ApprovalError("NOT_APPROVED")
            if slot.dispatched:
                raise ApprovalError("ALREADY_DISPATCHED")
            env = slot.envelope
            slot.dispatched = True  # one chance: a refusal below also spends it (ask again with a new quote)
            if presented_digest != slot.approval.envelope_digest or env.digest != slot.approval.envelope_digest:
                raise ApprovalError("TERMS_CHANGED")
            self._deadline(env)
            if env.quote is not None and (live_quote is None or _quote_core(live_quote) != _quote_core(env.quote)):
                raise ApprovalError("TERMS_CHANGED")
            return slot.approval

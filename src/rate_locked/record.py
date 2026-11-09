"""Approved-versus-executed record, and an offline check of an exported copy.

The record pairs, line by line, what the person approved (from the envelope) with what the provider reported
(from the conversion and transfer objects the adapter fetched). Match flags are computed by code. Provider
statuses are shown as reported; they are never given a match flag.

verify_record() uses only the exported dictionary. It checks the copy against its stored digest and recorded rows
(edits to the tested fields are detected). It does not authenticate the record or its source: give it the envelope digest the approver saw
(`expected_envelope_digest`) to detect a copy rebuilt end to end. "A view of our records, not a regulatory filing."
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Optional

from .approval import canonical_digest
from .models import Action

SCHEMA = "rate-locked.record.v1"
DASH = "-"


def _s(x) -> str:
    return DASH if x is None else (format(x, "f") if isinstance(x, Decimal) else str(x))


# field -> (approved value from the canonical envelope, executed value from the provider dict, kind)
def _q(c, key):
    return c["quote"][key]


CONVERSION_ROWS = {
    "pair": (lambda c: f"{_q(c, 'buy_currency')}/{_q(c, 'sell_currency')}",
             lambda p: f"{p['conversion']['buy_currency']}/{p['conversion']['sell_currency']}", "term"),
    "sell_amount": (lambda c: _q(c, "sell_amount"), lambda p: p["conversion"]["sell_amount"], "term"),
    "buy_amount": (lambda c: _q(c, "buy_amount"), lambda p: p["conversion"]["buy_amount"], "term"),
    "rate": (lambda c: _q(c, "client_rate"), lambda p: p["conversion"]["client_rate"], "term"),
    "quote_id": (lambda c: _q(c, "quote_id"), lambda p: p["conversion"]["quote_id"], "term"),
    "conversion_request_id": (lambda c: c["conversion_request_id"], lambda p: p["conversion"]["request_id"], "term"),
    "conversion_status": (lambda c: DASH, lambda p: p["conversion"]["status"], "status"),
}
PAYOUT_ROWS = {
    "payout_request_id": (lambda c: c["transfer_request_id"], lambda p: p["transfer"]["request_id"], "term"),
    "payout_amount": (lambda c: c["payout_amount"], lambda p: p["transfer"]["transfer_amount"], "term"),
    "payout_fee": (lambda c: c["payout_fee"], lambda p: p["transfer"]["fee_amount"], "term"),
    "beneficiary": (lambda c: c["beneficiary_id"], lambda p: p["transfer"]["beneficiary_id"], "term"),
    "payout_currency": (lambda c: c["payout_currency"], lambda p: p["transfer"]["transfer_currency"], "term"),
    "payout_source_currency": (lambda c: c["payout_currency"], lambda p: p["transfer"]["source_currency"], "term"),
    "payout_status": (lambda c: DASH, lambda p: p["transfer"]["status"], "status"),
}
NUMERIC_FIELDS = {"sell_amount", "buy_amount", "rate", "payout_amount", "payout_fee"}


def _rows_for(mode: str) -> dict:
    return {**(CONVERSION_ROWS if mode == Action.CONVERT.value else {}), **PAYOUT_ROWS}


def _same_field(field: str, a: str, b: str) -> bool:
    """Numbers compare as numbers; identifiers, currencies and statuses compare as exact strings."""
    return _same(a, b) if field in NUMERIC_FIELDS else a == b


def _same(a: str, b: str) -> bool:
    try:
        x, y = Decimal(a), Decimal(b)
    except (InvalidOperation, ValueError):
        return a == b
    if not (x.is_finite() and y.is_finite()):
        return a == b
    return x == y


@dataclass(frozen=True)
class Row:
    field: str
    approved: str
    executed: str
    matches: Optional[bool]  # None for provider statuses and for rows not executed yet

    def to_dict(self) -> dict:
        return {"field": self.field, "approved": self.approved, "executed": self.executed, "matches": self.matches}


@dataclass(frozen=True)
class ExecutionRecord:
    execution_id: str
    state: str
    envelope: dict
    envelope_digest: str
    provider: dict
    rows: tuple
    complete: bool
    all_match: bool
    call_counts: dict
    generated_at: str

    def to_dict(self) -> dict:
        d = {"schema": SCHEMA, "execution_id": self.execution_id, "state": self.state, "envelope": self.envelope,
             "envelope_digest": self.envelope_digest, "provider": self.provider,
             "rows": [r.to_dict() for r in self.rows], "complete": self.complete, "all_match": self.all_match,
             "call_counts": self.call_counts, "generated_at": self.generated_at}
        d["record_digest"] = record_digest_of(d)
        return d


def record_digest_of(d: dict) -> str:
    body = {k: v for k, v in d.items() if k != "record_digest"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def envelope_digest_of(canonical: dict) -> str:
    return canonical_digest(canonical)


def _provider_dict(ex) -> dict:
    conv, t = ex.conversion, ex.transfer
    return {
        "conversion": None if conv is None else {
            "conversion_id": conv.conversion_id, "request_id": conv.request_id, "status": conv.status,
            "sell_currency": conv.sell_currency, "buy_currency": conv.buy_currency,
            "sell_amount": _s(conv.sell_amount), "buy_amount": _s(conv.buy_amount),
            "client_rate": _s(conv.client_rate), "quote_id": conv.quote_id},
        "transfer": None if t is None else {
            "transfer_id": t.transfer_id, "request_id": t.request_id, "status": t.status,
            "beneficiary_id": t.beneficiary_id, "transfer_currency": t.transfer_currency, "source_currency": t.source_currency,
            "transfer_amount": _s(t.transfer_amount), "fee_amount": _s(t.fee_amount),
            # only when the reply named no beneficiary: the id is our own request's, not a provider echo
            **({"beneficiary_from": "request"} if getattr(t, "beneficiary_from", "reply") == "request" else {})},
    }


def _build_rows(canonical: dict, provider: dict) -> tuple:
    rows = []
    for name, (approved_fn, executed_fn, kind) in _rows_for(canonical["mode"]).items():
        group = "conversion" if name in CONVERSION_ROWS else "transfer"
        approved = _s(approved_fn(canonical))
        if provider.get(group) is None:
            rows.append(Row(name, approved, DASH, None))
            continue
        executed = _s(executed_fn(provider))
        rows.append(Row(name, approved, executed, None if kind == "status" else _same_field(name, approved, executed)))
    return tuple(rows)


def build_record(ex, call_counts: dict, now: datetime, envelope=None) -> ExecutionRecord:
    """`envelope` should be the gate's bound (approved) envelope; it defaults to the execution's own."""
    envelope = envelope or ex.envelope
    canonical = envelope.canonical()
    provider = _provider_dict(ex)
    rows = _build_rows(canonical, provider)
    complete = all(r.executed != DASH for r in rows)
    all_match = complete and all(r.matches is not False for r in rows)
    return ExecutionRecord(ex.execution_id, ex.state.value, canonical, envelope.digest, provider, rows,
                           complete, all_match, dict(call_counts), now.isoformat())


def verify_record(d: dict, expected_envelope_digest: Optional[str] = None) -> list:
    """Return a list of problems; empty means the exported copy matches its stored digest and recorded rows."""
    problems = []
    if d.get("schema") != SCHEMA:
        problems.append("SCHEMA unknown record format")
    if record_digest_of(d) != d.get("record_digest"):
        problems.append("RECORD_DIGEST does not match the content: the record was edited after export")
    canonical = d.get("envelope", {})
    if envelope_digest_of(canonical) != d.get("envelope_digest"):
        problems.append("ENVELOPE_DIGEST does not match the bound terms in the envelope")
    if expected_envelope_digest is not None and d.get("envelope_digest") != expected_envelope_digest:
        problems.append("ENVELOPE_DIGEST differs from the expected digest the approver saw")
    try:
        expected_rows = _rows_for(canonical["mode"])
    except KeyError:
        return problems + ["ENVELOPE missing mode"]
    provider = d.get("provider", {})
    seen = set()
    for r in d.get("rows", []):
        name = r.get("field")
        seen.add(name)
        if name not in expected_rows:
            problems.append(f"UNEXPECTED_ROW {name}")
            continue
        approved_fn, executed_fn, kind = expected_rows[name]
        group = "conversion" if name in CONVERSION_ROWS else "transfer"
        want_approved = _s(approved_fn(canonical))
        if not _same_field(name, r.get("approved", ""), want_approved):
            problems.append(f"{name.upper()}_MISMATCH at row {name}: approved column says {r.get('approved')}, "
                            f"the bound envelope says {want_approved}")
        if provider.get(group) is None:
            if r.get("executed") != DASH:
                problems.append(f"EXECUTED_WITHOUT_PROVIDER_DATA at row {name}")
            continue
        want_executed = _s(executed_fn(provider))
        if not _same_field(name, r.get("executed", ""), want_executed):
            problems.append(f"{name.upper()}_MISMATCH at row {name}: executed column says {r.get('executed')}, "
                            f"the provider data says {want_executed}")
        if kind == "term" and not _same_field(name, want_approved, want_executed):
            problems.append(f"{name.upper()}_MISMATCH at row {name}: approved {want_approved}, executed {want_executed}")
    for name in expected_rows:
        if name not in seen:
            problems.append(f"MISSING_ROW {name}")
    # derived claims are recomputed, never trusted
    try:
        fresh = _build_rows(canonical, provider)
    except (KeyError, TypeError):
        return problems + ["PROVIDER_DATA incomplete or malformed"]
    stored = {r.get("field"): r.get("matches") for r in d.get("rows", [])}
    for row in fresh:
        if row.field in stored and stored[row.field] != row.matches:
            problems.append(f"MATCHES flag at row {row.field} says {stored[row.field]}, recomputed {row.matches}")
    complete = all(r.executed != DASH for r in fresh)
    all_match = complete and all(r.matches is not False for r in fresh)
    if d.get("complete") != complete:
        problems.append(f"COMPLETE flag says {d.get('complete')}, recomputed {complete}")
    if d.get("all_match") != all_match:
        problems.append(f"ALL_MATCH flag says {d.get('all_match')}, recomputed {all_match}")
    return problems

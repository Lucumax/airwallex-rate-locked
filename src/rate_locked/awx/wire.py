"""Wire shapes: JSON <-> adapter types. ONE place for field names.

Field names follow the PUBLIC REFERENCE as read on 2026-11-01 (pages listed in
tests/fixtures/awx_docs/README.md); the sandbox answers recorded on 2026-11-05 matched for the fields they contain (see docs/AWX_API_SHAPES.md). Where this project's
first guess differed from the reference, the reference won: balances are a bare array and only `cash` lines count;
a rate's amounts live in rate_details[level=CLIENT]; a beneficiary's identifier is `id`; a transfer's response may
carry no beneficiary_id; amounts in conversion and transfer requests are strings; the transition body is
`next_status`; a simulated deposit names a `global_account_id`. When the probe disagrees with the reference, fix
it here and in nothing else (`python -m rate_locked check-shapes <probe json>` shows the differences). The local fake server emits these shapes.

Parse JSON with parse_float=Decimal (use `loads`) so amounts never pass through binary floats.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from .adapter import (Balance, Beneficiary, Conversion, Deposit, QuoteTerms, RateEstimate, Transfer)


def _no_constants(name):
    raise ValueError(f"non-finite number {name} is not accepted")


def loads(text: str):
    return json.loads(text, parse_float=Decimal, parse_constant=_no_constants)


def dumps(obj) -> str:
    """JSON with Decimals written as exact numbers (never through binary floats)."""
    if isinstance(obj, Decimal):
        if not obj.is_finite():
            raise ValueError("non-finite Decimal cannot be serialized")
        return format(obj, "f")
    if isinstance(obj, bool) or obj is None or isinstance(obj, (int, str)):
        return json.dumps(obj)
    if isinstance(obj, float):
        raise TypeError("use Decimal, not float, for amounts")
    if isinstance(obj, datetime):
        return json.dumps(ts(obj))
    if isinstance(obj, dict):
        return "{" + ",".join(json.dumps(str(k)) + ":" + dumps(v) for k, v in obj.items()) + "}"
    if isinstance(obj, (list, tuple)):
        return "[" + ",".join(dumps(v) for v in obj) + "]"
    raise TypeError(type(obj))


def ts(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+0000")


def parse_ts(s: str) -> datetime:
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    elif len(s) > 5 and s[-5] in "+-" and s[-3] != ":":
        s = s[:-2] + ":" + s[-2:]
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def dec(v) -> Decimal:
    d = Decimal(str(v))
    if not d.is_finite():
        raise ValueError("non-finite amount")
    return d


def _opt_ts(v) -> Optional[datetime]:
    return parse_ts(v) if v else None


# ---- balances -------------------------------------------------------------------------------
def balances_to_wire(items):
    return [{"account_type": "cash", "currency": b.currency, "available_amount": b.available, "pending_amount": 0,
             "prepayment_amount": 0, "reserved_amount": 0, "total_amount": b.available} for b in items]


def balances_from_wire(data):
    """The reference returns a bare array, one object per currency AND account type (cash, yield, credit). Only
    cash lines are spendable here; a line without account_type is read as cash."""
    return [Balance(d["currency"], dec(d["available_amount"])) for d in data
            if d.get("account_type", "cash") == "cash"]


# ---- rates ---------------------------------------------------------------------------------
def rate_to_wire(r: RateEstimate):
    return {"currency_pair": r.currency_pair, "rate": r.rate, "buy_currency": r.buy_currency,
            "sell_currency": r.sell_currency, "dealt_currency": r.buy_currency, "created_at": ts(r.created_at),
            "conversion_date": r.created_at.astimezone(timezone.utc).strftime("%Y-%m-%d"),
            "rate_details": [{"level": "CLIENT", "rate": r.rate, "buy_amount": r.buy_amount,
                              "sell_amount": r.sell_amount}]}


def rate_from_wire(d) -> RateEstimate:
    """Amounts come from rate_details[level == CLIENT]; the top-level `rate` is not trusted (the reference example
    shows a top-level rate that disagrees with the breakdown). No CLIENT level: unusable, never guessed."""
    detail = next((x for x in d["rate_details"] if x.get("level") == "CLIENT"), None)
    if detail is None:
        raise ValueError("rate_details has no CLIENT level")
    return RateEstimate(d["sell_currency"], d["buy_currency"], dec(detail["sell_amount"]), dec(detail["buy_amount"]),
                        d["currency_pair"], dec(detail["rate"]), parse_ts(d["created_at"]))


# ---- quotes ----------------------------------------------------------------------------------
def quote_to_wire(q: QuoteTerms):
    return {"quote_id": q.quote_id, "client_rate": q.client_rate, "currency_pair": q.currency_pair,
            "buy_currency": q.buy_currency, "sell_currency": q.sell_currency, "buy_amount": q.buy_amount,
            "sell_amount": q.sell_amount, "usage": q.usage, "valid_from_at": ts(q.valid_from_at),
            "valid_to_at": ts(q.valid_to_at)}


def quote_from_wire(d) -> QuoteTerms:
    return QuoteTerms(d["quote_id"], d["sell_currency"], d["buy_currency"], dec(d["sell_amount"]),
                      dec(d["buy_amount"]), dec(d["client_rate"]), parse_ts(d["valid_from_at"]),
                      parse_ts(d["valid_to_at"]), d.get("usage", "SINGLE_USE"), d.get("currency_pair", ""))


# ---- conversions ---------------------------------------------------------------------------
def conversion_to_wire(c: Conversion):
    return {"conversion_id": c.conversion_id, "request_id": c.request_id, "status": c.status,
            "sell_currency": c.sell_currency, "buy_currency": c.buy_currency, "sell_amount": str(c.sell_amount),
            "buy_amount": str(c.buy_amount), "client_rate": c.client_rate, "quote_id": c.quote_id,
            "created_at": ts(c.created_at) if c.created_at else None}


def conversion_from_wire(d) -> Conversion:
    return Conversion(d["conversion_id"], d["request_id"], d["status"], d["sell_currency"], d["buy_currency"],
                      dec(d["sell_amount"]), dec(d["buy_amount"]), dec(d["client_rate"]), d.get("quote_id"),
                      _opt_ts(d.get("created_at")))


def conversion_request_body(*, request_id, quote_id, buy_currency, sell_currency, buy_amount, sell_amount, reason=""):
    """POST /fx/conversions/create. Amounts are strings, exactly one of them. `reason` and `term_agreement` are not
    in the current reference for this call, so they are not sent (the reason stays in our own event log)."""
    body = {"request_id": request_id, "quote_id": quote_id, "buy_currency": buy_currency,
            "sell_currency": sell_currency}
    if buy_amount is not None:
        body["buy_amount"] = str(buy_amount)
    if sell_amount is not None:
        body["sell_amount"] = str(sell_amount)
    return body


# ---- beneficiaries and transfers -------------------------------------------------------------------
def beneficiary_create_body(*, request_id, beneficiary, transfer_methods=("LOCAL",)):
    """POST /beneficiaries/create. The reference marks `beneficiary` and `transfer_methods` as required; the
    contents of `beneficiary` depend on the corridor (see the schema calls in the probe), so the caller supplies it."""
    return {"request_id": request_id, "beneficiary": {"type": "BANK_ACCOUNT", **beneficiary},
            "transfer_methods": list(transfer_methods)}


def beneficiary_to_wire(b: Beneficiary):
    return {"id": b.beneficiary_id, "request_id": b.request_id, "nickname": "", "transfer_methods": ["LOCAL"],
            "beneficiary": dict(b.summary)}


def beneficiary_from_wire(d, request_id: str = "") -> Beneficiary:
    """The reference names the identifier `id`. The summary keeps only non-sensitive labels, never bank details."""
    inner = d.get("beneficiary") if isinstance(d.get("beneficiary"), dict) else {}
    bank = inner.get("bank_details") if isinstance(inner.get("bank_details"), dict) else {}
    pairs = (("name", inner.get("company_name") or inner.get("name") or d.get("nickname")),
             ("bank_country_code", bank.get("bank_country_code") or inner.get("bank_country_code")))
    return Beneficiary(d["id"], d.get("request_id") or request_id, {k: v for k, v in pairs if v})


def transfer_to_wire(t: Transfer):
    return {"id": t.transfer_id, "request_id": t.request_id, "status": t.status,
            "beneficiary_id": t.beneficiary_id, "source_currency": t.source_currency,
            "transfer_currency": t.transfer_currency, "transfer_amount": str(t.transfer_amount),
            "fee_amount": str(t.fee_amount), "created_at": ts(t.created_at) if t.created_at else None}


def transfer_from_wire(d, beneficiary_id: Optional[str] = None) -> Transfer:
    """The reference's transfer object lists no top-level beneficiary_id. Use one if present, else a nested
    beneficiary id, else the id our own request named, else "" (never invented). The executor compares it with the
    envelope's beneficiary before it adopts a looked-up record. `beneficiary_from` says which: "reply" when the
    provider named it, "request" when it is our own request's id (request context, not a provider echo)."""
    nested = d.get("beneficiary") if isinstance(d.get("beneficiary"), dict) else {}
    echoed = d.get("beneficiary_id") or nested.get("id") or nested.get("beneficiary_id")
    ben = echoed or beneficiary_id or ""
    return Transfer(d["id"], d["request_id"], d["status"], ben, d["source_currency"],
                    d["transfer_currency"], dec(d["transfer_amount"]), dec(d.get("fee_amount", 0)),
                    _opt_ts(d.get("created_at")), "reply" if echoed else "request")


def transfer_request_body(*, request_id, beneficiary_id, source_currency, transfer_currency, transfer_amount,
                          reason, reference, transfer_method="LOCAL"):
    """POST /transfers/create. Required by the reference: reason, reference, request_id, transfer_currency. The
    amount is a string and is the dealt side (transfer_amount), so source_amount is not sent."""
    return {"request_id": request_id, "beneficiary_id": beneficiary_id, "source_currency": source_currency,
            "transfer_currency": transfer_currency, "transfer_amount": str(transfer_amount), "reason": reason,
            "reference": reference, "transfer_method": transfer_method}


def transition_body(next_status: str) -> dict:
    """POST /simulation/transfers/{id}/transition (sandbox). The reference names the field `next_status`."""
    return {"next_status": next_status}


# ---- simulated deposits and global accounts ----------------------------------------------------------
def global_account_for(items, currency: str) -> Optional[str]:
    """Id of the first ACTIVE global account that can receive deposits in `currency` (the currency sits inside
    supported_features, not at the top level of an account)."""
    for a in items:
        if a.get("status") != "ACTIVE":
            continue
        if any(f.get("currency") == currency and f.get("type", "DEPOSIT") == "DEPOSIT"
               for f in a.get("supported_features", [])):
            return a["id"]
    return None


def deposit_request_body(*, global_account_id: str, amount) -> dict:
    """POST /simulation/deposit/create (sandbox). Required: amount (number) and global_account_id."""
    return {"global_account_id": global_account_id, "amount": amount}


def deposit_to_wire(x: Deposit):
    return {"id": x.deposit_id, "status": x.status, "currency": x.currency, "amount": x.amount,
            "deposit_type": "CREDIT"}


def deposit_from_wire(d, request_id: str = "") -> Deposit:
    """The reference's response carries no request_id; the one we generated is attached by the caller."""
    return Deposit(d["id"], request_id, d["currency"], dec(d["amount"]), d.get("status", "SETTLED"))


def error_to_wire(code: str, message: str) -> dict:
    return {"code": code, "message": message, "source": ""}

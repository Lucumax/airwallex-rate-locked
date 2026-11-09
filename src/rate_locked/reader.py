"""Invoice reader: the model reads, code decides.

A reader turns invoice text into five fields (supplier, invoice number, amount, currency, due date), each with the
verbatim source words it came from, and writes one plain-language explanation of a plan from structured facts.
It holds no credentials and has no decision field. Everything it returns is checked here by code:

  * every quote must occur verbatim in the invoice text; offsets are computed by code, never taken from the reader;
  * every value must be supported by its own quote (the amount, the currency code, the date parts);
  * a missing field, two disagreeing candidates, an unreadable value, an unexpected field or key, and the stop
    reasons `max_tokens` and `refusal` all send the invoice to review;
  * every number in an explanation must appear in the facts it was written from.

Readers in this repository: `ScriptedTestReader`, a TEST DOUBLE that returns hand-written replies and calls no
model. The live model reader is `model_reader.py`; any reader must implement `InvoiceReader`, send no sampling parameters
(temperature, top_p, top_k) to current models, use a generous output limit, and pass these same checks.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Optional, Protocol, runtime_checkable

FIELDS = ("supplier", "invoice_number", "amount", "currency", "due_date")
REPLY_KEYS = {"reader", "stop_reason", "fields"}
REJECTED_STOPS = ("max_tokens", "refusal", "unparseable", "api_error")  # the last two: a live reader's output was unusable or the call failed
MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
          "november", "december")


@runtime_checkable
class InvoiceReader(Protocol):
    label: str      # shown on the READER chip
    is_model: bool  # False for the test double

    def extract(self, invoice_id: str, text: str) -> dict:
        """Return {"reader", "stop_reason", "fields": {name: [{"value", "quote"}, ...]}}."""

    def explain(self, facts: list) -> dict:
        """Return {"reader", "stop_reason", "text"} written only from `facts`."""


@dataclass(frozen=True)
class FieldResult:
    name: str
    value: object            # parsed value (Decimal, datetime, str) or None
    raw: Optional[str]       # the value as the reader wrote it
    quote: Optional[str]
    start: Optional[int]     # offsets into the invoice text, computed by code
    end: Optional[int]
    flags: tuple

    @property
    def status(self) -> str:
        return "OK" if not self.flags else "REVIEW"


@dataclass(frozen=True)
class Extraction:
    reader: str
    stop_reason: str
    fields: tuple
    flags: tuple             # reply-level problems

    def field(self, name: str) -> FieldResult:
        return next(f for f in self.fields if f.name == name)

    @property
    def ready(self) -> bool:
        return not self.flags and all(f.status == "OK" for f in self.fields)


# ---- value parsing and support checks --------------------------------------------------------------

def _parse(name: str, raw: str):
    """Values must arrive as strings: a number, boolean or list is never coerced into an amount or a name."""
    if not isinstance(raw, str):
        return None
    if name == "amount":
        if not re.fullmatch(r"\d+(?:\.\d{1,2})?", raw):  # plain decimal, at most two places (demo currencies)
            return None
        v = Decimal(raw)
        return v if v > 0 else None
    if name == "due_date":
        try:
            v = datetime.fromisoformat(raw)
        except ValueError:
            return None
        return v if v.tzinfo is not None else None
    if name == "currency":
        return raw if re.fullmatch(r"[A-Z]{3}", raw) else None
    v = raw.strip()
    return v or None


_NUMBER = re.compile(r"(?<![\d.,/:-])\d[\d,]*(?:\.\d+)?(?![\d/:-])")
_ISO_DATE = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")
_DAY_FIRST = re.compile(r"(?<!\d)(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?([A-Za-z]{3,9})\.?,?\s+(\d{4})")
_MONTH_FIRST = re.compile(r"([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})")
_TIME = re.compile(r"(?<![\d:])(\d{1,2}):(\d{2})(?::\d{2})?\s*([ap]m)?", re.I)
_UTC_OFFSET = re.compile(r"\b(?:UTC|GMT)\s*([+-])(\d{1,2})(?::?(\d{2}))?")
_BARE_OFFSET = re.compile(r"\d\s*(?:[ap]m)?\s*([+-])(\d{2}):?(\d{2})(?!\d)", re.I)
_UTC_WORD = re.compile(r"\b(?:UTC|GMT)\b|(?<=\d)Z\b")


def _amounts_in(quote: str) -> set:
    """Every complete number in the quote, as an exact Decimal (thousands separators removed, nothing rounded)."""
    out = set()
    for m in _NUMBER.finditer(quote):
        token = m.group(0).rstrip(",")
        try:
            out.add(Decimal(token.replace(",", "")))
        except InvalidOperation:
            continue
    return out


def _month(name: str) -> Optional[int]:
    n = name.lower()
    for i, full in enumerate(MONTHS):
        if len(n) >= 3 and full.startswith(n[:3]) and (full.startswith(n) or n == "sept"):
            return i + 1
    return None


def _dates_in(quote: str) -> set:
    out = {(int(y), int(m), int(d)) for y, m, d in _ISO_DATE.findall(quote)}
    for d, mon, y in _DAY_FIRST.findall(quote):
        if _month(mon):
            out.add((int(y), _month(mon), int(d)))
    for mon, d, y in _MONTH_FIRST.findall(quote):
        if _month(mon):
            out.add((int(y), _month(mon), int(d)))
    return out


def _times_in(quote: str) -> set:
    out = set()
    for h, mi, ap in _TIME.findall(quote):
        h, mi = int(h), int(mi)
        if ap:
            if not 1 <= h <= 12:
                continue
            h = h % 12 + (12 if ap.lower() == "pm" else 0)
        if h < 24 and mi < 60:
            out.add((h, mi))
    return out


def _stated_offset(quote: str):
    """The UTC offset the quote states: a timedelta, or None when it states none."""
    m = _UTC_OFFSET.search(quote)
    if m:
        sign = -1 if m.group(1) == "-" else 1
        return sign * timedelta(hours=int(m.group(2)), minutes=int(m.group(3) or 0))
    if _UTC_WORD.search(quote):
        return timedelta(0)
    m = _BARE_OFFSET.search(quote)
    if m:
        sign = -1 if m.group(1) == "-" else 1
        return sign * timedelta(hours=int(m.group(2)), minutes=int(m.group(3)))
    return None


def _whole_word(value: str, quote: str) -> bool:
    return re.search(r"(?<![A-Za-z0-9])" + re.escape(value) + r"(?![A-Za-z0-9])", quote, re.I) is not None


def _supported(name: str, value, quote: str) -> bool:
    if name == "amount":
        return value in _amounts_in(quote)  # a whole number in the quote, exactly equal; "200.00" is not in "1,200.00"
    if name == "currency":
        return re.search(rf"\b{value}\b", quote) is not None
    if name == "due_date":
        d = value
        if (d.year, d.month, d.day) not in _dates_in(quote):
            return False
        times = _times_in(quote)
        return (d.hour, d.minute) in times if times else (d.hour, d.minute) == (0, 0)
    return _whole_word(str(value), quote)


def _check_field(name: str, text: str, candidates) -> FieldResult:
    if not candidates:
        return FieldResult(name, None, None, None, None, None, ("MISSING",))
    flags = []
    parsed = []
    for c in candidates:
        if not isinstance(c, dict):
            flags.append("UNREADABLE_VALUE")
            continue
        parsed.append((c.get("value"), _parse(name, c.get("value")), c.get("quote")))
    if not parsed:
        return FieldResult(name, None, None, None, None, None, tuple(dict.fromkeys(flags)))
    distinct = {p for _, p, _ in parsed if p is not None}
    if len(distinct) > 1:
        flags.append("CONFLICT")
    raw, value, quote = parsed[0]
    start = end = None
    for i, (_, v, q) in enumerate(parsed):  # every candidate is checked, not only the first one
        if v is None:
            flags.append("UNREADABLE_VALUE")
        if not isinstance(q, str) or not q or q not in text:
            flags.append("QUOTE_NOT_VERBATIM")
            continue
        if i == 0:
            start = text.index(q)
            end = start + len(q)
        if v is not None and not _supported(name, v, q):
            flags.append("VALUE_NOT_IN_QUOTE")
        if name == "due_date" and v is not None and _times_in(q):
            stated = _stated_offset(q)
            if stated is None:
                flags.append("TIMEZONE_NOT_STATED")
            elif v.utcoffset() != stated:
                flags.append("TIMEZONE_MISMATCH")
    return FieldResult(name, value, None if raw is None else str(raw), quote if isinstance(quote, str) else None,
                       start, end, tuple(dict.fromkeys(flags)))


def check_resolution(name: str, value, quote: Optional[str]):
    """A person's answer to a review flag. Returns (parsed value, problem code or None).

    The person decides what the source words leave open (for a due date: the time zone); the answer must still be
    supported by the verbatim source words, so a person cannot type in a different day, time or amount here.
    """
    parsed = _parse(name, value)
    if parsed is None:
        return None, "UNREADABLE_VALUE"
    if not quote:
        return None, "NO_SOURCE_WORDS"
    if not _supported(name, parsed, quote):
        return None, "VALUE_NOT_IN_QUOTE"
    return parsed, None


def validate_reply(text: str, reply: dict) -> Extraction:
    """Check a reader reply against the invoice text. Never raises on bad reader output: it flags it."""
    flags = []
    if not isinstance(reply, dict):
        reply = {}
        flags.append("UNREADABLE_REPLY")
    for key in sorted(set(reply) - REPLY_KEYS):
        flags.append(f"UNEXPECTED_KEY {key}")
    stop = str(reply.get("stop_reason", ""))
    if stop in REJECTED_STOPS:
        flags.append(f"READER_STOPPED {stop}")
    fields = reply.get("fields") if isinstance(reply.get("fields"), dict) else {}
    for key in sorted(set(fields) - set(FIELDS)):
        flags.append(f"UNEXPECTED_FIELD {key}")
    results = tuple(_check_field(n, text, fields.get(n) or []) for n in FIELDS)
    return Extraction(str(reply.get("reader", "")), stop, results, tuple(flags))


def read_invoice(reader: InvoiceReader, invoice) -> Extraction:
    return validate_reply(invoice.text, reader.extract(invoice.invoice_id, invoice.text))


# ---- explanation check -------------------------------------------------------------------------------

_NUM = re.compile(r"(?<![\w.,])\d[\d,]*(?:\.\d+)?")


def _numbers(s: str) -> set:
    return {m.group(0).rstrip(",") for m in _NUM.finditer(s)}


def check_explanation(text: str, facts: list) -> list:
    """Problems with an explanation; empty means every number in it appears in the facts."""
    known = set()
    for f in facts:
        known |= _numbers(f)
    return [f"NUMBER_NOT_IN_FACTS {n}" for n in sorted(_numbers(text) - known)]


# ---- the test double ---------------------------------------------------------------------------------

class ScriptedTestReader:
    """TEST DOUBLE. Returns hand-written replies and a templated explanation. Calls no model."""

    label = "TEST DOUBLE (scripted, no model)"
    is_model = False

    def __init__(self, script: Optional[dict] = None):
        if script is None:
            from .invoices import SCRIPTED_REPLIES
            script = SCRIPTED_REPLIES
        self._script = script

    def extract(self, invoice_id: str, text: str) -> dict:
        fields = self._script.get(invoice_id)
        if fields is None:
            return {"reader": self.label, "stop_reason": "end_turn", "fields": {}}
        return {"reader": self.label, "stop_reason": "end_turn",
                "fields": {k: [dict(c) for c in v] for k, v in fields.items()}}

    def explain(self, facts: list) -> dict:
        lines = []
        for f in facts:
            head, _, why = f.partition(": ")
            lines.append(f"{head}. {why}".strip())
        return {"reader": self.label, "stop_reason": "end_turn", "text": " ".join(lines)}

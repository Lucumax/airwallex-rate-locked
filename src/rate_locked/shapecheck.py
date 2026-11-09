"""check-shapes: run the parsers in awx/wire.py over the responses a sandbox probe recorded.

    python -m rate_locked check-shapes evidence/probe_<time>.json

The probe saves sanitized responses per step. This reads that file (never the network, never keys), feeds each
successful step's response to the parser the product uses for that endpoint, and says which ones still match. A
MISMATCH row names the step and the field the parser could not find: fix `awx/wire.py` (and the fake) from it.
OK means "the parser accepted the recorded answer", not "the provider's behavior is proven".
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from .awx.wire import (balances_from_wire, beneficiary_from_wire, conversion_from_wire, deposit_from_wire,
                       global_account_for, loads, quote_from_wire, rate_from_wire, transfer_from_wire)


@dataclass(frozen=True)
class Row:
    step: str
    name: str
    status: str   # OK | MISMATCH | NOT_CHECKED
    detail: str


def _list_of(parse):
    def go(d):
        if not isinstance(d, dict) or not isinstance(d.get("items"), list):
            raise KeyError("items")
        for x in d["items"]:
            parse(x)
        return len(d["items"])
    return go


def _need_account(d):
    if not isinstance(d, dict) or not isinstance(d.get("items"), list):
        raise KeyError("items")
    if global_account_for(d["items"], "__none__") is not None:
        raise ValueError("unexpected account match")
    return len(d["items"])


# step id -> (what it is, parser applied to the step's response)
CHECKS = {
    "P2": ("balances", balances_from_wire),
    "P4b": ("current rate", rate_from_wire),
    "P5": ("create quote", quote_from_wire),
    "P6": ("retrieve quote", quote_from_wire),
    "P7": ("create conversion", conversion_from_wire),
    "P9": ("conversion lookup", _list_of(conversion_from_wire)),
    "P11": ("create beneficiary", beneficiary_from_wire),
    "P12": ("create transfer", transfer_from_wire),
    "P13": ("transfer lookup", _list_of(transfer_from_wire)),
    "P14": ("transfer SENT", transfer_from_wire),
    "P15": ("transfer PAID", transfer_from_wire),
    "P16": ("retrieve transfer", transfer_from_wire),
    "SIM0": ("global accounts", _need_account),
    "SIM": ("simulated deposit", deposit_from_wire),
}


def check_evidence(evidence: dict) -> list:
    rows = []
    for s in evidence.get("steps", []):
        sid = s.get("id")
        if sid not in CHECKS:
            continue
        name, parse = CHECKS[sid]
        if not s.get("ok"):
            rows.append(Row(sid, name, "NOT_CHECKED", f"step did not succeed (HTTP {s.get('http_status')})"))
            continue
        try:
            resp = s.get("response")
            if isinstance(resp, list):  # the probe's sanitizer ends a long list with a "[N more items not shown]" string
                resp = [x for x in resp if not (isinstance(x, str) and x.endswith("more items not shown]"))]
            parse(loads(json.dumps(resp)))
        except (KeyError, TypeError, ValueError, ArithmeticError, AttributeError, StopIteration) as e:
            # field names and error types only: an exception's own text can repeat the offending value
            what = f"missing or unreadable field {e.args[0]!r}" if isinstance(e, KeyError) and e.args else f"{type(e).__name__} while reading a field"
            rows.append(Row(sid, name, "MISMATCH", what))
            continue
        rows.append(Row(sid, name, "OK", "parser accepted the recorded answer"))
    return rows


def render(rows: list) -> str:
    lines = [f"{'Step':<5} {'Endpoint':<20} {'Result':<12} Detail"]
    lines += [f"{r.step:<5} {r.name:<20} {r.status:<12} {r.detail}" for r in rows]
    bad = [r for r in rows if r.status == "MISMATCH"]
    if bad:
        verdict = "MISMATCH on " + ", ".join(r.step for r in bad) + ": fix awx/wire.py and the fake from these, then re-run the tests."
    elif not any(r.status == "OK" for r in rows):
        verdict = "NOTHING CHECKED: no step with a recorded answer."
    else:
        verdict = "every recorded answer was accepted by the parsers."
    return "\n".join(lines + ["", "Verdict: " + verdict])


def main(path: str) -> int:
    try:
        ev = json.loads(open(path, encoding="utf-8").read())
    except (OSError, ValueError) as e:
        print(f"Cannot read {path}: {type(e).__name__}")
        return 2
    rows = check_evidence(ev)
    print(render(rows))
    return 1 if any(r.status == "MISMATCH" for r in rows) else 0

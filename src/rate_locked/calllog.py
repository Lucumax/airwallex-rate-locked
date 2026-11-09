"""Transport log (an adapter-attempt log): wraps any adapter and records every call attempt before it is delegated,
so "Conversion calls: 0" comes from a count kept apart from the code that decided not to send. A counted attempt is
not proof that a request reached the provider (it can fail before anything is transmitted); zero attempts means the
adapter was never asked to send."""
from __future__ import annotations

import threading
from dataclasses import dataclass, replace
from typing import Optional

from .awx.adapter import AirwallexError, UncertainOutcome

FINANCIAL_OPS = ("create_conversion", "create_transfer")


@dataclass(frozen=True)
class Call:
    seq: int
    op: str
    request_id: Optional[str]
    outcome: str  # ok | error:<code> | uncertain


class CallLog:
    def __init__(self, inner):
        self._inner = inner
        self.log: list = []
        self._lock = threading.Lock()

    def __getattr__(self, name):
        attr = getattr(self._inner, name)
        if not callable(attr):
            return attr

        def wrapper(*args, **kwargs):
            rid = kwargs.get("request_id")
            idx = self._record(name, rid, "started")  # the attempt is counted before it is delegated
            try:
                result = attr(*args, **kwargs)
            except UncertainOutcome:
                self._set(idx, "uncertain")
                raise
            except AirwallexError as e:
                self._set(idx, f"error:{e.code}")
                raise
            except Exception:
                self._set(idx, "error:unexpected")
                raise
            self._set(idx, "ok")
            return result

        return wrapper

    def _record(self, op: str, rid: Optional[str], outcome: str) -> int:
        with self._lock:
            self.log.append(Call(len(self.log) + 1, op, rid, outcome))
            return len(self.log) - 1

    def _set(self, idx: int, outcome: str) -> None:
        with self._lock:
            self.log[idx] = replace(self.log[idx], outcome=outcome)

    def count(self, op: str, request_id: Optional[str] = None) -> int:
        return sum(1 for c in self.log if c.op == op and (request_id is None or c.request_id == request_id))

"""Execution state machine: from an approval request to a reported payout.

Money rules, each enforced in code and covered by tests:
  * Nothing reaches a financial endpoint before a person approved the exact envelope and the gate authorized
    dispatch (one chance per approval).
  * A stale or changed approval makes zero conversion and zero payout calls (counted by an independent
    transport log, calllog.CallLog).
  * An intent event is written BEFORE each financial call (write-ahead). If the outcome is uncertain the state
    becomes UNKNOWN and the only way forward is a lookup by the pre-minted request id. A create call is never
    repeated. Empty lookups are not proof of absence: after MAX_EMPTY_LOOKUPS they become INDETERMINATE
    (needs a person) and the lookup stays open.
  * Provider statuses are kept verbatim; PROCESSING is never shown as PAID. PAID is the provider's report, not a
    final settlement: the lifecycle page says a PAID transfer can still end FAILED, so a PAID execution can be
    checked again (on request; nothing polls in the background) and a late failure becomes PAYOUT_FAILED with its
    own events (nothing is re-sent).
  * A settled conversion is compared with the approved quote (quote id, currencies, both amounts, rate, request id)
    before the payout; a difference stops the execution as INDETERMINATE for a person, with no payout at that point.
    A later lookup or poll that finds the approved terms resumes it (no acknowledgement step).
  * A transfer the provider reports (create reply, poll or lookup) is compared with the approved payout (request id,
    beneficiary, currencies, amount, fee) before PAID is shown; a difference holds it as INDETERMINATE for a person
    (with a same-request failure status: PAYOUT_FAILED). After PAID, a difference stays PAID with a needs-a-person
    event. A later clean lookup can clear INDETERMINATE to PAID. A poll that returns another transfer id is never
    adopted. Nothing is re-sent.
  * Operations on one execution are serialized (a lock per execution), so overlapping dispatch, advance and
    reconcile calls cannot send the same request twice. The approval deadline is re-checked immediately
    before the first financial send, after any logging or balance reads.
  * Recovery scope: state lives IN MEMORY. "Progress lost" handling covers a lost in-process record, not a
    process restart. What survives a restart is a GUARD, not recovery: with an event log file, every dispatch intent
    is on disk (flushed and synced) before its call, and a new process that loads the log (`load_unresolved`) refuses
    to prepare a new approval for an obligation whose earlier create was never seen to end, because a new approval
    would mint a new request id and could pay twice. A person looks the request id up at the provider, then clears
    the log deliberately. Real restart support would need persisted envelopes, consumed approvals and replay.
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

from .approval import (DEFAULT_TTL, Approval, ApprovalError, ApprovalGate, Envelope, EnvelopeError, MESSAGES,
                       build_envelope)
from .awx.adapter import AirwallexError, QuoteTerms, UncertainOutcome, means_lookup
from .calllog import CallLog
from .models import Action, Obligation
from .planner import Decision

MAX_EMPTY_LOOKUPS = 3
# Quote validities the console offers. The approval window follows the quote (approval.build_envelope takes the
# earlier of quote end minus the dispatch margin and now plus ttl), so ttl is never shorter than the quote.
VALIDITY_SECONDS = {"MIN_1": 60, "MIN_15": 900, "HR_1": 3600}
TRANSFER_FAILED = ("FAILED", "CANCELLED", "RETURNED")


class State(str, Enum):
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    APPROVED = "APPROVED"
    DENIED = "DENIED"
    EXPIRED = "EXPIRED"
    VOIDED = "VOIDED"
    CONVERTING = "CONVERTING"
    FUNDS_READY = "FUNDS_READY"          # converted, or already held; supplier payment not yet sent
    PAYING = "PAYING"
    PAYOUT_REPORTED = "PAYOUT_REPORTED"  # provider status shown verbatim
    PAID = "PAID"
    PAYOUT_FAILED = "PAYOUT_FAILED"
    CONVERSION_FAILED = "CONVERSION_FAILED"
    UNKNOWN = "UNKNOWN"                  # outcome of a create call is uncertain: look up, never resend
    INDETERMINATE = "INDETERMINATE"      # lookups stayed empty: needs a person, no second payment is sent


S = State
NON_LIVE = frozenset({S.DENIED, S.EXPIRED, S.VOIDED, S.CONVERSION_FAILED, S.PAYOUT_FAILED})
ACTIVE = frozenset({S.CONVERTING, S.FUNDS_READY, S.PAYING, S.PAYOUT_REPORTED, S.PAID})
TRANSITIONS: dict = {
    S.AWAITING_APPROVAL: frozenset({S.APPROVED, S.DENIED, S.EXPIRED, S.VOIDED}),
    S.APPROVED: frozenset({S.CONVERTING, S.FUNDS_READY, S.EXPIRED, S.VOIDED}),
    S.CONVERTING: frozenset({S.FUNDS_READY, S.CONVERSION_FAILED, S.UNKNOWN, S.EXPIRED, S.INDETERMINATE}),
    S.FUNDS_READY: frozenset({S.PAYING}),
    S.PAYING: frozenset({S.PAYOUT_REPORTED, S.PAID, S.PAYOUT_FAILED, S.UNKNOWN, S.EXPIRED, S.INDETERMINATE}),
    S.PAYOUT_REPORTED: frozenset({S.PAID, S.PAYOUT_FAILED, S.INDETERMINATE}),
    S.UNKNOWN: frozenset({S.CONVERTING, S.PAYOUT_REPORTED, S.PAID, S.PAYOUT_FAILED, S.CONVERSION_FAILED,
                          S.INDETERMINATE}),
    S.INDETERMINATE: frozenset({S.CONVERTING, S.PAYOUT_REPORTED, S.PAID, S.PAYOUT_FAILED, S.CONVERSION_FAILED}),
    S.PAID: frozenset({S.PAYOUT_FAILED}),  # reported, not final: a PAID transfer can still fail late
    S.DENIED: frozenset(), S.EXPIRED: frozenset(), S.VOIDED: frozenset(),
    S.CONVERSION_FAILED: frozenset(), S.PAYOUT_FAILED: frozenset(),
}

def conversion_terms_differ(quote, conv, request_id) -> list:
    """The fields where a settled conversion differs from the approved quote (numbers compare as numbers). An empty
    list means the provider reported exactly the approved terms; anything else stops the execution before the payout."""
    out = []
    for f in ("quote_id", "sell_currency", "buy_currency"):
        if getattr(conv, f) != getattr(quote, f):
            out.append(f)
    for f in ("sell_amount", "buy_amount", "client_rate"):
        try:
            same = Decimal(str(getattr(conv, f))) == Decimal(str(getattr(quote, f)))
        except Exception:  # noqa: BLE001 - an unreadable number is a difference
            same = False
        if not same:
            out.append(f)
    if conv.request_id != request_id:
        out.append("request_id")
    return out


def transfer_terms_differ(env, t) -> list:
    """The fields where a transfer the provider reports differs from the approved payout. An empty list means the
    compared fields equal the approved values (a beneficiary id the adapter filled from the request is not a provider
    echo); anything else is shown to a person instead of PAID. The transfer was already sent when this runs."""
    out = []
    for f, want in (("request_id", env.transfer_request_id), ("beneficiary_id", env.beneficiary_id),
                    ("source_currency", env.payout_currency), ("transfer_currency", env.payout_currency)):
        if getattr(t, f) != want:
            out.append(f)
    for f, want in (("transfer_amount", env.payout_amount), ("fee_amount", env.payout_fee)):
        try:
            same = Decimal(str(getattr(t, f))) == Decimal(str(want))
        except Exception:  # noqa: BLE001 - an unreadable number is a difference
            same = False
        if not same:
            out.append(f)
    return out


STATE_TEXT = {
    S.AWAITING_APPROVAL: "Waiting for a person to approve the exact terms.",
    S.APPROVED: "Approved. Not sent yet.",
    S.DENIED: MESSAGES["DENIED"],
    S.EXPIRED: "The approval window closed or the rate expired. Nothing was converted.",
    S.VOIDED: MESSAGES["TERMS_CHANGED"],
    S.CONVERTING: "Converting with the approved quote.",
    S.FUNDS_READY: "Funds ready. Supplier payment not yet sent.",
    S.PAYING: "Sending the supplier payment.",
    S.PAYOUT_REPORTED: "Payout reported by the provider.",
    S.PAID: "Paid, as reported by the provider. A paid transfer can still fail, so this is checked again.",
    S.PAYOUT_FAILED: "Payment failed. Not retried. A person decides.",
    S.CONVERSION_FAILED: "Conversion failed. No payment was sent. A person decides.",
    S.UNKNOWN: "Not sure yet. Checking by request ID.",
    S.INDETERMINATE: "Still unresolved. No second payment will be sent. Needs review.",
}


class StateError(Exception):
    pass


@dataclass(frozen=True)
class Event:
    seq: int
    at: datetime
    execution_id: str
    kind: str
    data: dict


class EventLog:
    """Append-only. With a path, every event is also written as one JSON line and flushed before returning."""

    def __init__(self, path: Optional[Path] = None):
        self.events: list = []
        self.path = Path(path) if path else None
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def append(self, execution_id: str, at: datetime, kind: str, data: dict) -> Event:
        with self._lock:
            ev = Event(len(self.events) + 1, at, execution_id, kind, data)
            self.events.append(ev)
            if self.path:
                line = json.dumps({"seq": ev.seq, "at": at.isoformat(), "execution_id": execution_id, "kind": kind,
                                   **data}, default=str, sort_keys=True)
                with open(self.path, "a", encoding="utf-8", newline="\n") as fh:
                    fh.write(line + "\n")
                    fh.flush()
                    os.fsync(fh.fileno())  # the intent must be on disk before the financial call is made
            return ev


_RESOLVES = {"conversion": ("conversion_created", "conversion_refused"), "payout": ("transfer_created", "payout_refused")}
_DISPATCH = {"conversion_dispatching": "conversion", "transfer_dispatching": "payout"}


def unresolved_intents(path) -> dict:
    """Dispatch intents in an event log that were never seen to end: {obligation_id: [{stage, request_id, execution_id}]}.
    An intent ends when the provider answered (created or refused), a lookup found the request, or the send was
    aborted before leaving. An unreadable log raises StateError: the guard fails closed."""
    p = Path(path)
    if not p.exists():
        return {}
    open_by_exec: dict = {}
    for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            ev = json.loads(line)
            kind, eid = ev["kind"], ev["execution_id"]
        except (ValueError, KeyError, TypeError):
            raise StateError(f"the event log {p.name} is unreadable at line {n}; it cannot be trusted") from None
        if kind in _DISPATCH:
            open_by_exec[eid] = {"stage": _DISPATCH[kind], "request_id": ev.get("request_id"),
                                 "execution_id": eid, "obligation_id": ev.get("obligation_id")}
        elif eid in open_by_exec:
            stage = open_by_exec[eid]["stage"]
            if kind in _RESOLVES[stage] or kind == "dispatch_aborted_before_send" or (
                    kind == "lookup_found" and ev.get("stage") == stage):
                del open_by_exec[eid]
    out: dict = {}
    for item in open_by_exec.values():
        out.setdefault(item["obligation_id"], []).append(
            {k: item[k] for k in ("stage", "request_id", "execution_id")})
    return out


class Execution:
    """One approval request and what became of it. The envelope is read-only: it is what the approver saw."""

    def __init__(self, execution_id: str, envelope: Envelope, decision: Decision, obligation: Obligation,
                 supersedes: Optional[str] = None):
        self.execution_id = execution_id
        self._envelope = envelope
        self.decision = decision
        self.obligation = obligation
        self.supersedes = supersedes
        self.superseded_by: Optional[str] = None
        self.state = State.AWAITING_APPROVAL
        self.events: list = []
        self.conversion = None
        self.transfer = None
        self.approval: Optional[Approval] = None
        self.unknown_stage: Optional[str] = None
        self.empty_lookups = 0
        self.failure_seen = False    # a lookup showed a failure of this request: a later PAID waits for a person
        self.validity = "MIN_1"  # the quote validity this request asked for; a requote asks for the same

    @property
    def envelope(self) -> Envelope:
        return self._envelope

    @property
    def text(self) -> str:
        if self.state is S.EXPIRED and self._envelope.quote is None:  # a FUND request has no rate, only a window
            return "The approval window closed. Nothing was paid."
        return STATE_TEXT[self.state]


@dataclass(frozen=True)
class Prepared:
    execution: Execution
    token: str  # shown once to the approver page; the gate keeps only a hash


@dataclass(frozen=True)
class ApprovalOutcome:
    ok: bool
    code: str
    message: str
    execution: Optional[Execution]


@dataclass(frozen=True)
class QuoteDifference:
    currency: str
    old_sell: Decimal
    new_sell: Decimal
    delta: Decimal
    direction: str  # more | less | none

    @property
    def text(self) -> str:
        if self.direction == "none":
            return "No change in cost"
        return f"Difference: {self.currency} {abs(self.delta):,.2f} {self.direction}"


class Executor:
    def __init__(self, adapter, gate: ApprovalGate, clock, *, log: Optional[EventLog] = None,
                 ids: Callable[[], str] = lambda: uuid.uuid4().hex):
        self.calls = CallLog(adapter)
        self.gate = gate
        self.clock = clock
        self.log = log or EventLog()
        self.ids = ids
        self.executions: dict = {}
        self._locks: dict = {}
        self._locks_guard = threading.Lock()
        self._live: dict = {}  # obligation id -> execution id of the one live request for it
        self.blocked: dict = {}  # obligation id -> unresolved intents left by an earlier process
        self._log_path = None

    def load_unresolved(self, path) -> None:
        """Restart guard: read the event log of earlier runs and refuse new approvals where a create never ended."""
        self._log_path = Path(path)
        self.blocked = unresolved_intents(path)

    def _lock(self, execution_id: str):
        with self._locks_guard:
            return self._locks.setdefault(execution_id, threading.RLock())

    def _warm_up(self) -> None:
        """Let the adapter finish slow setup (such as logging in) BEFORE the final deadline check."""
        fn = getattr(self.calls._inner, "warm_up", None)
        if fn:
            try:
                fn()
            except Exception:  # noqa: BLE001 - the real call will surface a real error
                pass

    def _deadline_ok(self, ex: "Execution") -> bool:
        """Last check before a financial send. Payout of funds already converted is not re-gated by expiry."""
        if ex.conversion is not None:
            return True
        try:
            self.gate.recheck(ex.execution_id)
            return True
        except ApprovalError as e:
            self._emit(ex, "dispatch_aborted_before_send", code=e.code)
            self._transition(ex, State.EXPIRED, f"approval no longer valid at send time: {e.code}")
            return False

    # ---- plumbing --------------------------------------------------------------------------------
    def _get(self, execution_id: str) -> Execution:
        return self.executions[execution_id]

    def _emit(self, ex: Execution, kind: str, **data) -> None:
        ev = self.log.append(ex.execution_id, self.clock.now(), kind, data)
        ex.events.append(ev)

    def _emit_once(self, ex: Execution, kind: str, **data) -> None:
        """A review request is raised once per distinct difference; each observation is still logged separately."""
        if not any(e.kind == kind and e.data == data for e in ex.events):
            self._emit(ex, kind, **data)

    def _transition(self, ex: Execution, to: State, why: str) -> None:
        if to not in TRANSITIONS[ex.state]:
            raise StateError(f"{ex.state.value} -> {to.value} is not allowed ({why})")
        self._emit(ex, "state", **{"from": ex.state.value, "to": to.value, "why": why})
        ex.state = to

    def _force_state(self, ex: Execution, to: State) -> None:
        """Test hook to simulate a crash that lost in-memory progress. Not used by product code."""
        ex.state = to

    def call_counts(self, execution_id: str) -> dict:
        env = self._get(execution_id).envelope
        conv = self.calls.count("create_conversion", env.conversion_request_id) if env.conversion_request_id else 0
        return {"conversion_create": conv, "transfer_create": self.calls.count("create_transfer", env.transfer_request_id)}

    def lapse_overdue(self) -> None:
        """A request nobody answered is EXPIRED once its approval deadline passes (it is not an ask that stays open
        forever). The approval deadline is `margin` before the quote ends, so this is not the quote's own expiry."""
        now = self.clock.now()
        for ex in list(self.executions.values()):
            if ex.state is State.AWAITING_APPROVAL and now >= ex.envelope.expires_at:
                with self._lock(ex.execution_id):
                    if ex.state is State.AWAITING_APPROVAL:
                        self._transition(ex, State.EXPIRED, "no approval before the approval deadline")

    # ---- prepare and approve -------------------------------------------------------------------------
    def prepare(self, decision: Decision, obligation: Obligation, plan_digest: str, *, validity: str = "MIN_1",
                supersedes: Optional[str] = None) -> Prepared:
        if decision.action not in (Action.FUND, Action.CONVERT):
            raise EnvelopeError("NOT_EXECUTABLE", f"{decision.action.value} decisions are never executed by the agent")
        left = self.blocked.get(obligation.obligation_id)
        if left:
            ids = ", ".join(f"{i['stage']} request {i['request_id']}" for i in left)
            raise StateError(f"an earlier run left an unresolved {ids} for obligation {obligation.obligation_id}. "
                             "It may be live at the provider: look it up by that request id in the provider's "
                             "dashboard first, then clear the event log "
                             f"({self._log_path.name if self._log_path else 'event log'}) deliberately. "
                             "No new approval is requested while it is open.")
        self.lapse_overdue()
        with self._locks_guard:
            prior = self._live.get(obligation.obligation_id)
            if prior is not None and self.executions[prior].state not in NON_LIVE:
                raise StateError(f"obligation {obligation.obligation_id} already has a live request ({prior}); "
                                 "finish, deny or let it lapse first")
        quote = None
        if decision.action is Action.CONVERT:
            quote = self.calls.create_quote(buy_currency=obligation.currency, sell_currency=decision.sell_currency,
                                            validity=validity, buy_amount=decision.convert_buy_amount)
        ttl = max(DEFAULT_TTL, timedelta(seconds=VALIDITY_SECONDS.get(validity, 0)))
        env = build_envelope(decision, obligation, quote, now=self.clock.now(), plan_digest=plan_digest, ttl=ttl,
                             dispatch_margin=self.gate.dispatch_margin, ids=self.ids)
        token = self.gate.issue(env)
        ex = Execution(env.envelope_id, env, decision, obligation, supersedes=supersedes)
        ex.validity = validity
        with self._locks_guard:  # check again under the guard: two overlapping prepares cannot both win
            prior = self._live.get(obligation.obligation_id)
            if prior is not None and self.executions[prior].state not in NON_LIVE:
                raise StateError(f"obligation {obligation.obligation_id} already has a live request ({prior})")
            self.executions[ex.execution_id] = ex
            self._live[obligation.obligation_id] = ex.execution_id
        if supersedes:
            self.executions[supersedes].superseded_by = ex.execution_id
        self._emit(ex, "approval_requested", digest=env.digest, mode=env.mode.value,
                   quote_id=quote.quote_id if quote else None, expires_at=env.expires_at.isoformat())
        return Prepared(ex, token)

    def approve(self, token: str, presented_digest: str, approver_id: str) -> ApprovalOutcome:
        try:
            env = self.gate.peek(token)
        except ApprovalError as e:
            return ApprovalOutcome(False, e.code, e.message, None)
        with self._lock(env.envelope_id):
            ex = self._get(env.envelope_id)
            try:
                approval = self.gate.approve(token, presented_digest, approver_id)
            except ApprovalError as e:
                self._emit(ex, "approval_refused", code=e.code)
                if ex.state is State.AWAITING_APPROVAL:
                    if e.code == "EXPIRED":
                        self._transition(ex, State.EXPIRED, "approval attempted after the approval window closed")
                    elif e.code == "TERMS_CHANGED":
                        self._transition(ex, State.VOIDED, "presented terms differ from the request")
                return ApprovalOutcome(False, e.code, e.message, ex)
            ex.approval = approval
            self._transition(ex, State.APPROVED, "approved by a person")
            self._emit(ex, "approved", approver=approval.approver_id, digest=approval.envelope_digest)
            return ApprovalOutcome(True, "APPROVED", "Approved.", ex)

    def deny(self, token: str, approver_id: str) -> Execution:
        env = self.gate.peek(token)
        with self._lock(env.envelope_id):
            ex = self._get(env.envelope_id)
            self.gate.deny(token, approver_id)
            self._transition(ex, State.DENIED, "denied by a person")
            self._emit(ex, "denied", approver=approver_id)
            return ex

    def requote(self, execution_id: str):
        """Ask again after a lapsed or voided approval. Allowed only if no financial call was ever attempted."""
        with self._lock(execution_id):
            ex = self._get(execution_id)
            if ex.state not in (State.EXPIRED, State.VOIDED) or ex.envelope.mode is not Action.CONVERT:
                raise StateError("a new quote can be requested only after an expired or voided conversion approval")
            if ex.superseded_by is not None:
                raise StateError("this request was already replaced by a newer one; use that one")
            counts = self.call_counts(execution_id)
            if counts["conversion_create"] or counts["transfer_create"]:
                raise StateError("a financial call was already attempted for this request")
            old = ex.envelope.quote
            prep = self.prepare(ex.decision, ex.obligation, ex.envelope.plan_digest, validity=ex.validity,
                                supersedes=ex.execution_id)
            new = prep.execution.envelope.quote
            delta = new.sell_amount - old.sell_amount
            direction = "none" if delta == 0 else ("more" if delta > 0 else "less")
            diff = QuoteDifference(new.sell_currency, old.sell_amount, new.sell_amount, delta, direction)
            self._emit(ex, "requoted", new_execution=prep.execution.execution_id, old_sell=str(old.sell_amount),
                       new_sell=str(new.sell_amount), delta=str(delta))
            return prep, diff

    # ---- dispatch -------------------------------------------------------------------------------------
    def _have(self, currency: str):
        return {b.currency: b.available for b in self.calls.get_balances()}.get(currency, Decimal(0))

    def dispatch(self, execution_id: str) -> Execution:
        with self._lock(execution_id):
            return self._dispatch(execution_id)

    def _dispatch(self, execution_id: str) -> Execution:
        ex = self._get(execution_id)
        if ex.state is not State.APPROVED:
            self._emit(ex, "dispatch_ignored", state=ex.state.value)
            return ex
        env = self.gate.bound_envelope(execution_id)  # the approved snapshot, not the mutable object
        # Preparatory reads happen BEFORE authorization so they cannot eat into the approval window.
        self._warm_up()
        live: Optional[QuoteTerms] = None
        try:
            if env.quote is not None:
                live = self.calls.get_quote(env.quote.quote_id)
            else:
                have = self._have(env.payout_currency)
                if have < env.payout_amount + env.payout_fee:
                    self._emit(ex, "payout_waiting_for_funds", have=str(have),
                               need=str(env.payout_amount + env.payout_fee))
                    return ex  # nothing consumed; try again when funds are there
        except (AirwallexError, UncertainOutcome) as e:
            self._emit(ex, "approval_refused", code="PREPARATION_FAILED", detail=type(e).__name__)
            if env.quote is not None:
                self.gate.void(execution_id)
                self._transition(ex, State.VOIDED, "the locked quote could not be re-read before dispatch")
            return ex
        try:
            self.gate.authorize(execution_id, live, ex.envelope.digest)
        except ApprovalError as e:
            self._emit(ex, "approval_refused", code=e.code)
            to = State.EXPIRED if e.code in ("EXPIRED", "TOO_CLOSE_TO_EXPIRY") else State.VOIDED
            self._transition(ex, to, f"dispatch refused: {e.code}")
            return ex
        if env.mode is Action.CONVERT:
            self._transition(ex, State.CONVERTING, "dispatch authorized")
            self._emit(ex, "conversion_dispatching", request_id=env.conversion_request_id, quote_id=env.quote.quote_id,
                       obligation_id=ex.obligation.obligation_id)
            if not self._deadline_ok(ex):
                return ex
            try:
                conv = self.calls.create_conversion(
                    request_id=env.conversion_request_id, quote_id=env.quote.quote_id,
                    buy_currency=env.quote.buy_currency, sell_currency=env.quote.sell_currency,
                    buy_amount=env.quote.buy_amount, reason=f"{env.invoice_ref} supplier payment")
            except UncertainOutcome as e:
                self._to_unknown(ex, "conversion", e.detail)
                return ex
            except AirwallexError as e:
                if means_lookup(e):  # request id already known or still pending: it may exist, so look it up
                    self._emit(ex, "conversion_request_known", code=e.code)
                    self._to_unknown(ex, "conversion", e.code)
                    return ex
                self._emit(ex, "conversion_refused", code=e.code)
                self._transition(ex, State.EXPIRED if e.code == "expired_quote" else State.CONVERSION_FAILED,
                                 f"provider refused the conversion: {e.code}")
                return ex
            except Exception as e:  # noqa: BLE001 - anything unexpected after a send is treated as uncertain
                self._to_unknown(ex, "conversion", f"unexpected {type(e).__name__}")
                return ex
            ex.conversion = conv
            self._emit(ex, "conversion_created", conversion_id=conv.conversion_id, status=conv.status,
                       quote_id=conv.quote_id, client_rate=str(conv.client_rate))
        else:
            self._transition(ex, State.FUNDS_READY, "dispatch authorized; existing balance covers the payout")
        return self._advance(ex)

    def _to_unknown(self, ex: Execution, stage: str, detail: str = "") -> None:
        ex.unknown_stage = stage
        ex.empty_lookups = 0
        self._transition(ex, State.UNKNOWN, f"{stage} outcome uncertain")
        self._emit(ex, "outcome_unknown", stage=stage, detail=detail)

    # ---- progress ---------------------------------------------------------------------------------------
    def advance(self, execution_id: str) -> Execution:
        with self._lock(execution_id):
            return self._advance(self._get(execution_id))

    def _bound(self, ex: Execution):
        """The approved envelope as the GATE holds it, or None (and an event) if it cannot be trusted.
        Fails closed: no approval at the gate, or an execution object that no longer matches it, halts progress."""
        try:
            env, approval = self.gate.bound(ex.execution_id)
        except ApprovalError:
            self._emit(ex, "approval_missing_halted")
            return None
        if ex.envelope.digest != approval.envelope_digest or env.digest != approval.envelope_digest:
            self._emit(ex, "envelope_mismatch_halted")
            return None
        return env

    def record(self, execution_id: str):
        """Approved-versus-executed record built from the gate's bound envelope, never the mutable object."""
        from .record import build_record
        ex = self._get(execution_id)
        try:
            env = self.gate.bound_envelope(execution_id)
        except ApprovalError:
            env = ex.envelope
        return build_record(ex, self.call_counts(execution_id), self.clock.now(), envelope=env)

    def _advance(self, ex: Execution) -> Execution:
        if ex.state not in ACTIVE:
            return ex
        env = self._bound(ex)
        if env is None:
            return ex
        for _ in range(8):
            s = ex.state
            if s is State.CONVERTING:
                if ex.conversion is None:  # progress was lost: do not create again, look it up
                    self._to_unknown(ex, "conversion", "no conversion record in memory")
                    return ex
                try:
                    conv = self.calls.get_conversion(ex.conversion.conversion_id)
                except (AirwallexError, UncertainOutcome):
                    self._emit(ex, "poll_failed", what="conversion")
                    return ex
                ex.conversion = conv
                if conv.status == "SETTLED":
                    differ = conversion_terms_differ(env.quote, conv, env.conversion_request_id)
                    if differ:  # the provider settled other terms than the person approved: stop before the payout
                        self._emit(ex, "conversion_terms_differ", fields=differ, conversion_id=conv.conversion_id)
                        self._emit(ex, "needs_person_after_conversion", conversion_id=conv.conversion_id)
                        ex.unknown_stage = "conversion"
                        self._transition(ex, State.INDETERMINATE,
                                         "the settled conversion differs from the approved quote: " + ", ".join(differ))
                        return ex
                    self._transition(ex, State.FUNDS_READY, "conversion settled")
                    continue
                if conv.status in ("CANCELLED", "OVERDUE"):
                    self._transition(ex, State.CONVERSION_FAILED, f"conversion {conv.status}")
                return ex
            if s is State.FUNDS_READY:
                need = env.payout_amount + env.payout_fee
                self._warm_up()
                try:
                    have = self._have(env.payout_currency)
                except (AirwallexError, UncertainOutcome):
                    self._emit(ex, "poll_failed", what="balances")
                    return ex
                if have < need:
                    self._emit(ex, "payout_waiting_for_funds", have=str(have), need=str(need))
                    return ex
                self._transition(ex, State.PAYING, "funds confirmed available")
                self._emit(ex, "transfer_dispatching", request_id=env.transfer_request_id,
                           obligation_id=ex.obligation.obligation_id)
                if not self._deadline_ok(ex):
                    return ex
                try:
                    t = self.calls.create_transfer(
                        request_id=env.transfer_request_id, beneficiary_id=env.beneficiary_id,
                        source_currency=env.payout_currency, transfer_currency=env.payout_currency,
                        transfer_amount=env.payout_amount, reason=f"{env.invoice_ref} supplier payment",
                        reference=env.invoice_ref, transfer_method=env.payout_method)
                except UncertainOutcome as e:
                    self._to_unknown(ex, "payout", e.detail)
                    return ex
                except AirwallexError as e:
                    if means_lookup(e):  # request id already known or still pending: the payment may be live
                        self._emit(ex, "payout_request_known", code=e.code)
                        self._to_unknown(ex, "payout", e.code)
                        return ex
                    self._emit(ex, "payout_refused", code=e.code)
                    if ex.conversion is not None:
                        self._emit(ex, "needs_person_after_conversion", conversion_id=ex.conversion.conversion_id)
                    self._transition(ex, State.PAYOUT_FAILED, f"provider refused the payout: {e.code}")
                    return ex
                except Exception as e:  # noqa: BLE001
                    self._to_unknown(ex, "payout", f"unexpected {type(e).__name__}")
                    return ex
                ex.transfer = t
                self._emit(ex, "transfer_created", transfer_id=t.transfer_id, status=t.status)
                self._apply_transfer(ex)
                return ex
            if s is State.PAYING:
                if ex.transfer is None:
                    self._to_unknown(ex, "payout", "no transfer record in memory")
                return ex
            if s in (State.PAYOUT_REPORTED, State.PAID):
                try:
                    t = self.calls.get_transfer(ex.transfer.transfer_id)
                except (AirwallexError, UncertainOutcome):
                    self._emit(ex, "poll_failed", what="transfer")
                    return ex
                if t.transfer_id != ex.transfer.transfer_id:  # never adopt another transfer: keep watching ours
                    self._emit(ex, "poll_returned_other_transfer", asked=ex.transfer.transfer_id,
                               got=t.transfer_id, status=t.status)
                    if s is State.PAID:  # PAID stays (no exit but a confirmed failure); a person looks
                        self._emit_once(ex, "needs_person_after_paid", transfer_id=t.transfer_id, fields=["transfer_id"])
                        return ex
                    self._emit_once(ex, "needs_person_after_payout", transfer_id=t.transfer_id, fields=["transfer_id"])
                    ex.unknown_stage = "payout"
                    self._transition(ex, State.INDETERMINATE,
                                     f"a poll for {ex.transfer.transfer_id} returned {t.transfer_id}")
                    return ex
                ex.transfer = t
                if s is State.PAID:
                    self._emit(ex, "transfer_polled_after_paid", status=ex.transfer.status)
                self._apply_transfer(ex)
                return ex
            return ex
        return ex

    def _apply_transfer(self, ex: Execution) -> None:
        status = ex.transfer.status
        env = self._bound(ex)
        differ = transfer_terms_differ(env, ex.transfer) if env is not None else ["approval"]
        if differ:  # the provider reports other terms than the person approved: a person looks, never PAID
            self._emit(ex, "transfer_terms_differ", fields=differ, transfer_id=ex.transfer.transfer_id, status=status)
            if ex.state is State.PAID:
                self._emit_once(ex, "needs_person_after_paid", transfer_id=ex.transfer.transfer_id, fields=differ)
                if "request_id" in differ or status not in TRANSFER_FAILED:
                    return
                # the same request now reports a failure: show the confirmed late failure, not a stale PAID
                self._emit(ex, "late_failure_after_paid", status=status, transfer_id=ex.transfer.transfer_id)
                self._transition(ex, State.PAYOUT_FAILED, f"provider reports {status} after PAID")
                return
            self._emit_once(ex, "needs_person_after_payout", transfer_id=ex.transfer.transfer_id, fields=differ)
            if "request_id" not in differ and status in TRANSFER_FAILED:
                # the same request reports a failure: keep it (terminal), so a later matching PAID cannot clear it
                self._transition(ex, State.PAYOUT_FAILED,
                                 f"provider reports {status}, with other terms: " + ", ".join(differ))
                return
            ex.unknown_stage = "payout"
            if ex.state is not State.INDETERMINATE:
                self._transition(ex, State.INDETERMINATE,
                                 "the provider's transfer differs from the approved payout: " + ", ".join(differ))
            return
        if ex.state is State.PAID:  # reported, not final: only a failure or a surprise changes anything
            if status in TRANSFER_FAILED:
                self._emit(ex, "late_failure_after_paid", status=status, transfer_id=ex.transfer.transfer_id)
                self._emit(ex, "needs_person_after_paid", transfer_id=ex.transfer.transfer_id)
                self._transition(ex, State.PAYOUT_FAILED, f"provider reports {status} after PAID")
            elif status != "PAID":
                self._emit(ex, "status_changed_after_paid", status=status)
            return
        if status == "PAID" and ex.failure_seen:  # contradicts a failure seen earlier for this request
            self._emit_once(ex, "needs_person_after_payout", transfer_id=ex.transfer.transfer_id, fields=["status"])
            if ex.state is not State.INDETERMINATE:
                self._transition(ex, State.INDETERMINATE, "PAID reported after a failure was seen for this request")
            return
        if status == "PAID":
            self._transition(ex, State.PAID, "provider reports PAID")
        elif status in TRANSFER_FAILED:
            self._transition(ex, State.PAYOUT_FAILED, f"provider reports {status}")
        elif ex.state is not State.PAYOUT_REPORTED:
            self._transition(ex, State.PAYOUT_REPORTED, f"provider reports {status}")

    # ---- reconcile by lookup ----------------------------------------------------------------------------
    def reconcile(self, execution_id: str) -> Execution:
        with self._lock(execution_id):
            return self._reconcile(execution_id)

    def _reconcile(self, execution_id: str) -> Execution:
        ex = self._get(execution_id)
        if ex.state not in (State.UNKNOWN, State.INDETERMINATE):
            self._emit(ex, "reconcile_ignored", state=ex.state.value)
            return ex
        env = self._bound(ex)
        if env is None:
            return ex
        stage = ex.unknown_stage
        try:
            if stage == "conversion":
                raw = self.calls.find_conversions(env.conversion_request_id)
                found = [c for c in raw if c.request_id == env.conversion_request_id
                         and c.quote_id == env.quote.quote_id]
            else:
                raw = self.calls.find_transfers(env.transfer_request_id, beneficiary_id=env.beneficiary_id)
                # matched by request id only: a different beneficiary is a term difference (_apply_transfer flags it),
                # so a failure that comes with it is kept, not dropped as a foreign record
                found = [t for t in raw if t.request_id == env.transfer_request_id]
        except (AirwallexError, UncertainOutcome):
            self._emit(ex, "lookup_failed", stage=stage)
            return ex
        if raw and not found:  # records came back, but none belongs to this request: never adopt them
            self._emit(ex, "lookup_anomaly", stage=stage, count=len(raw), reason="foreign records only")
            if ex.state is State.UNKNOWN:
                self._transition(ex, State.INDETERMINATE, "lookup returned only records of other requests")
            return ex
        if len(found) > 1:
            self._emit(ex, "lookup_anomaly", stage=stage, count=len(found))
            if stage != "conversion" and any(t.status in TRANSFER_FAILED for t in found):
                ex.failure_seen = True
            if ex.state is State.UNKNOWN:
                self._transition(ex, State.INDETERMINATE, "more than one record for one request id")
            return ex
        if not found:
            ex.empty_lookups += 1
            self._emit(ex, "lookup_empty", stage=stage, count=ex.empty_lookups)
            if ex.empty_lookups >= MAX_EMPTY_LOOKUPS and ex.state is State.UNKNOWN:
                self._transition(ex, State.INDETERMINATE, "lookups stayed empty; no resend")
            return ex
        self._emit(ex, "lookup_found", stage=stage)
        ex.empty_lookups = 0
        if stage == "conversion":
            ex.conversion = found[0]
            self._transition(ex, State.CONVERTING, "conversion found by request id")
            return self._advance(ex)
        ex.transfer = found[0]
        self._apply_transfer(ex)
        return ex

"""One console session: the adapter, approval gate, executor, invoice reader, plan history and run manifest.

The web console (server.py) is a thin JSON face over this class. It reuses the core unchanged: `make_plan`,
`replan`, `Executor.prepare / approve / deny / requote / dispatch / advance / reconcile / record`,
`verify_record` and `call_counts`. Every adapter call goes through the executor's transport log, so the call
counts on screen come from what was actually sent.

Every value handed to the screens carries its source: FAKE (the local fake of the Airwallex endpoints), SANDBOX
(the Airwallex sandbox), FIXTURE (fictional inputs), TEST DOUBLE (the scripted reader) or CODE (computed
here). Test controls (moving the clock, moving a fake rate, withholding a response) exist only in the FAKE
world and are logged.

An invoice whose fields go to review stops there, and so does the plan: until a person resolves every review flag
on the console (a value the source words support, plus a required note, labelled HUMAN), no plan is built and
nothing is quoted, converted or paid.
"""
from __future__ import annotations

import re
import secrets
import threading
import uuid
from dataclasses import dataclass, field, replace
from datetime import timezone
from decimal import Decimal, InvalidOperation
from typing import Optional

from .approval import ApprovalError, ApprovalGate, EnvelopeError, MESSAGES
from .awx.adapter import AirwallexError, UncertainOutcome
from .executor import ACTIVE, VALIDITY_SECONDS, EventLog, Executor, State, StateError
from .invoices import INVOICES
from .models import Action, Confidence, Obligation
from .orchestrate import read_balances, refresh_balances, snapshot_inputs
from .plancard import _approval, _uses
from .planner import Plan, PlanInputs, ReplanResult, make_plan, replan
from .reader import FIELDS, REJECTED_STOPS, ScriptedTestReader, check_explanation, check_resolution, read_invoice
from .record import verify_record
from .scenario import demo_inputs

DIGEST_PREFIX = 8
APPROVER_ID = "approver-page"
MONEY_MAY_HAVE_MOVED = frozenset({State.CONVERTING, State.FUNDS_READY, State.PAYING, State.PAYOUT_REPORTED,
                                  State.PAID, State.PAYOUT_FAILED, State.UNKNOWN, State.INDETERMINATE})
WITHHOLDABLE = ("create_transfer", "create_conversion")
TRANSITIONS = ("PAID", "FAILED", "CANCELLED", "PROCESSING")
OPERATOR_ID = "console operator"
NOTE_MAX = 200
FIELD_LABELS = {"supplier": "supplier", "invoice_number": "invoice number", "amount": "amount",
                "currency": "currency", "due_date": "due date"}


class SessionError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.status = status


def m(amount: Decimal) -> str:
    return f"{amount:,.2f}"


def in_words(env) -> str:
    """One plain sentence of exactly what an approval covers, built from the bound envelope (CODE, no model)."""
    q = env.quote
    pay = f"pay {env.supplier_name} {env.payout_currency} {m(env.payout_amount)} for invoice {env.invoice_ref}"
    if env.mode is Action.FUND or q is None:
        return f"{pay[0].upper() + pay[1:]} from the {env.payout_currency} balance. No conversion."
    held = ""
    if env.existing_balance_used:
        held = f" ({env.payout_currency} {m(env.existing_balance_used)} of it comes from the balance already held)"
    return (f"Convert {q.sell_currency} {m(q.sell_amount)} into {q.buy_currency} {m(q.buy_amount)} at the locked quote "
            f"{q.quote_id} (rate {format(q.client_rate, 'f')}), then {pay}{held}.")


def _iso(dt) -> Optional[str]:
    return None if dt is None else dt.isoformat()


def _plain(v):
    if v is None or isinstance(v, (bool, int, str)):
        return v
    return str(v)


@dataclass
class Stage:
    title: str
    trigger: list
    plan: Plan
    result: Optional[ReplanResult] = None
    diff: list = field(default_factory=list)
    explanation: dict = field(default_factory=dict)


def input_diff(old: PlanInputs, new: PlanInputs, ceiling_amount) -> list:
    out = []
    for c in sorted(set(old.balances) | set(new.balances)):
        a, b = old.balances.get(c, Decimal("0")), new.balances.get(c, Decimal("0"))
        if a != b:
            out.append({"field": f"{c} balance", "before": m(a), "after": m(b)})
    old_r = {r.receipt_id: r for r in old.receipts}
    for r in new.receipts:
        o = old_r.get(r.receipt_id)
        if o is not None and o.confidence != r.confidence:
            out.append({"field": f"{r.receipt_id} confidence", "before": o.confidence.value, "after": r.confidence.value})
    from .planner import ceiling_level
    lo, ln = ceiling_level(old), ceiling_level(new)
    if lo != ln:
        out.append({"field": "Execution limit (demo policy)",
                    "before": f"{old.policy.base_currency} {m(ceiling_amount(old.policy, lo))} ({lo.value.lower()} forecast confidence)",
                    "after": f"{new.policy.base_currency} {m(ceiling_amount(new.policy, ln))} ({ln.value.lower()} forecast confidence)"})
    return out


def _cap(policy, level: Confidence) -> Decimal:
    return policy.ceiling_high if level is Confidence.HIGH else policy.ceiling_low


class Session:
    def __init__(self, adapter, *, world: str, clock, reader=None, fake_controls=None,
                 run_id: Optional[str] = None, commit: Optional[str] = None,
                 beneficiaries: Optional[dict] = None, plan_now=None, event_log_path: Optional[str] = None):
        if world not in ("FAKE", "SANDBOX"):
            raise ValueError("world must be FAKE or SANDBOX")
        if world != "FAKE" and fake_controls is not None:
            raise ValueError("test controls exist only in the FAKE world")
        self.world = world
        self.clock = clock
        self.gate = ApprovalGate(clock)
        if event_log_path:  # write-ahead events on disk; a restart refuses new approvals where a create never ended
            self.executor = Executor(adapter, self.gate, clock, log=EventLog(event_log_path))
            self.executor.load_unresolved(event_log_path)
        else:
            self.executor = Executor(adapter, self.gate, clock)
        self.calls = self.executor.calls
        self.reader = reader or ScriptedTestReader()
        self._fake = fake_controls
        self.run_id = run_id or "r-" + secrets.token_hex(4)
        self.commit = commit or "uncommitted"
        self.started_at = clock.now()
        self._beneficiaries = dict(beneficiaries or {})  # supplier name -> beneficiary payload (sandbox: from the owner's file)
        self._plan_now = plan_now                          # pins planning time to the fixture's dates; quotes use the real clock
        self._lock = threading.RLock()
        self.tokens: dict = {}
        self.requotes: dict = {}
        self.controls_log: list = []
        self.stages: list = []
        self.invoices: list = []
        self.obligations: list = []
        self.reviews: dict = {}      # invoice_id -> {"status": "PENDING" | "RESOLVED", "resolutions": {field: ...}}
        self.resolved: list = []     # every resolution, in order
        self._seed = demo_inputs()
        self._balances_seen = ({}, clock.now())
        self._read_invoices()
        if not self._pending():
            self._start_plan()

    # ---- setup --------------------------------------------------------------------------------------
    def _src(self) -> str:
        return "FAKE" if self.world == "FAKE" else "SANDBOX"

    def _read_invoices(self) -> None:
        for inv in INVOICES:
            ex = read_invoice(self.reader, inv)
            self.invoices.append((inv, ex))
            if ex.ready:
                self._add_obligation(inv, {f.name: f.value for f in ex.fields})
            else:
                self.reviews[inv.invoice_id] = {"status": "PENDING", "resolutions": {}}  # the planner never sees it

    def _add_obligation(self, inv, v: dict) -> None:
        payload = self._beneficiaries.get(v["supplier"], {"name": v["supplier"]})
        ben = self.calls.create_beneficiary(request_id=f"ben-{self.run_id}-{inv.obligation_id}", payload=payload)
        self.obligations.append(Obligation(inv.obligation_id, v["supplier"], ben.beneficiary_id, v["amount"],
                                           v["currency"], v["due_date"], invoice_ref=v["invoice_number"]))
        order = [i.obligation_id for i in INVOICES]
        self.obligations.sort(key=lambda o: order.index(o.obligation_id))

    def _pending(self) -> list:
        return [i for i, r in self.reviews.items() if r["status"] == "PENDING"]

    def _start_plan(self) -> None:
        inputs = snapshot_inputs(self.calls, self.obligations, self._seed.receipts, self._seed.policy,
                                 self._plan_now or self.clock.now())
        self._balances_seen = (dict(inputs.balances), self.clock.now())
        trigger = [f"Balances and indicative rates read from the {self._src()} world."]
        trigger += [r["trigger"] for r in self.resolved]
        self._push(Stage("Plan v1: baseline", trigger, make_plan(inputs)))

    # ---- review: a person resolves what the reader could not --------------------------------------------
    def resolve_review(self, invoice_id: str, field_name: str, value, note) -> dict:
        with self._lock:
            found = [(inv, ex) for inv, ex in self.invoices if inv.invoice_id == invoice_id]
            if not found:
                raise SessionError("UNKNOWN_INVOICE", f"no invoice {invoice_id}", 404)
            inv, ex = found[0]
            if field_name not in FIELDS:
                raise SessionError("UNKNOWN_FIELD", f"field must be one of {FIELDS}", 400)
            review = self.reviews.get(invoice_id)
            if review is None or review["status"] != "PENDING":
                raise SessionError("NOT_IN_REVIEW", f"{invoice_id} is not waiting for review", 409)
            if ex.flags:
                raise SessionError("REPLY_REJECTED", "The reader's reply was rejected as a whole; read the invoice "
                                   "again.", 409)
            f = ex.field(field_name)
            if f.status == "OK" or field_name in review["resolutions"]:
                raise SessionError("FIELD_NOT_FLAGGED", f"{field_name} has no open review flag", 409)
            note = str(note or "").strip()
            if not note:
                raise SessionError("NOTE_REQUIRED", "Say how you checked this value.", 400)
            if len(note) > NOTE_MAX:
                raise SessionError("NOTE_TOO_LONG", f"Keep the note to {NOTE_MAX} characters or fewer.", 400)
            parsed, problem = check_resolution(field_name, value, f.quote if f.start is not None else None)
            if problem == "NO_SOURCE_WORDS":
                raise SessionError(problem, "This field has no verbatim source words to check an answer against; "
                                   "read the invoice again.", 409)
            if problem == "VALUE_NOT_IN_QUOTE":
                raise SessionError(problem, "The answer must keep the date and time in the source words; only "
                                   "what they leave open is yours to decide.", 400)
            if problem:
                raise SessionError(problem, "That value cannot be read (a due date needs a time zone offset).", 400)
            shown = parsed.astimezone(timezone.utc) if field_name == "due_date" else parsed
            res = {"invoice_id": invoice_id, "obligation_id": inv.obligation_id, "field": field_name,
                   "value": _iso(shown) if field_name == "due_date" else str(shown), "entered": str(value),
                   "note": note, "by": OPERATOR_ID, "at": _iso(self.clock.now()), "flags_before": list(f.flags),
                   "quote": f.quote}
            ref = ex.field("invoice_number").value or invoice_id
            if field_name == "due_date":
                off = parsed.strftime("%z")
                res["trigger"] = (f"{ref} {FIELD_LABELS[field_name]} confirmed by a person: \"{f.quote}\" read as "
                                  f"UTC{off[:3]}:{off[3:]}, due {shown:%d %b %H:%M} UTC. Note: {note}")
            else:
                res["trigger"] = f"{ref} {FIELD_LABELS[field_name]} confirmed by a person: {shown}. Note: {note}"
            review["resolutions"][field_name] = {"parsed": shown, **res}
            self.resolved.append(res)
            open_fields = [x.name for x in ex.fields if x.status != "OK" and x.name not in review["resolutions"]]
            if not open_fields:
                v = {x.name: x.value for x in ex.fields}
                v.update({k: r["parsed"] for k, r in review["resolutions"].items()})
                n_obligations = len(self.obligations)
                try:
                    self._add_obligation(inv, v)
                    review["status"] = "RESOLVED"
                    if not self._pending():
                        self._start_plan()
                except (AirwallexError, UncertainOutcome) as e:
                    # setup failed: undo this answer so the same answer can be sent again (the beneficiary request
                    # id is fixed per obligation, so a retry cannot create a second beneficiary)
                    review["status"] = "PENDING"
                    del review["resolutions"][field_name]
                    self.resolved.pop()
                    self.obligations[:] = [o for o in self.obligations if o.obligation_id != inv.obligation_id]                         if len(self.obligations) > n_obligations else self.obligations
                    raise SessionError("SETUP_FAILED", f"The answer was not saved because setup failed ({e}). "
                                       "Send it again.", 502) from None
            return {"invoice_id": invoice_id, "field": field_name, "ready": review["status"] == "RESOLVED",
                    "open_fields": open_fields, "pending": self._pending(),
                    "plan_version": self.stages[-1].plan.version if self.stages else None}

    def _explain(self, plan: Plan) -> dict:
        names = {o.obligation_id: o.supplier_name for o in plan.inputs.obligations}
        facts = [f"{d.obligation_id} {names[d.obligation_id]} {d.action.value}: {d.reason_text}" for d in plan.decisions]
        out = self.reader.explain(facts)
        text = str(out.get("text", ""))
        problems = check_explanation(text, facts)
        stop = str(out.get("stop_reason", ""))
        if stop in REJECTED_STOPS:
            problems.append(f"READER_STOPPED {stop}")
        return {"reader": self.reader.label, "text": text, "facts": facts, "problems": problems,
                "accepted": not problems}

    def _push(self, stage: Stage) -> None:
        stage.explanation = self._explain(stage.plan)
        self.stages.append(stage)

    @property
    def plan(self) -> Plan:
        if not self.stages:
            n = len(self._pending())
            raise SessionError("REVIEW_PENDING", f"{n} invoice{'s' if n != 1 else ''} need{'' if n != 1 else 's'} "
                               "a person's review on the Invoices screen before the plan can be built.", 409)
        return self.stages[-1].plan

    def _refresh_balances(self) -> None:
        try:
            self._balances_seen = (read_balances(self.calls), self.clock.now())
        except (AirwallexError, UncertainOutcome):
            pass

    def _replan(self, title: str, trigger: list, new_inputs: PlanInputs) -> dict:
        prev = self.plan
        # an obligation whose request is already converting, paying or paid is satisfied: the provider balance
        # already reflects it, so it must not reserve money again (earlier plan versions keep it)
        # (a request that later fails brings its obligation back: the set is rebuilt from the first plan each time)
        active = {ex.obligation.obligation_id for ex in self.executor.executions.values() if ex.state in ACTIVE}
        first = self.stages[0].plan.inputs.obligations if self.stages else new_inputs.obligations
        have = {o.obligation_id: o for o in new_inputs.obligations}
        known = {o.obligation_id for o in first}
        obligations = tuple(have.get(o.obligation_id, o) for o in first if o.obligation_id not in active)
        obligations += tuple(o for o in new_inputs.obligations if o.obligation_id not in known and o.obligation_id not in active)
        new_inputs = replace(new_inputs, obligations=obligations)
        # money may have moved once any conversion or payout create was attempted (even one whose outcome is unknown):
        # plan from the provider's balances, and say so if they cannot be read
        moved = any(sum(self.executor.call_counts(eid).values()) for eid in self.executor.executions)
        if moved:
            try:
                new_inputs = refresh_balances(self.calls, new_inputs)
                self._balances_seen = (dict(new_inputs.balances), self.clock.now())
            except (AirwallexError, UncertainOutcome):
                trigger = list(trigger) + ["Balances could not be read again after a payment was sent; "
                                           "this plan uses the balances read earlier and needs a check"]
        res = replan(prev, new_inputs)
        self._push(Stage(title, trigger, res.plan, res, input_diff(prev.inputs, new_inputs, _cap)))
        return self._plan_json(self.stages[-1])

    # ---- triggers -----------------------------------------------------------------------------------
    def lower_confidence(self, receipt_id: str) -> dict:
        with self._lock:
            found = [r for r in self.plan.inputs.receipts if r.receipt_id == receipt_id]
            if not found:
                raise SessionError("UNKNOWN_RECEIPT", f"no receipt {receipt_id}", 404)
            r = found[0]
            if r.confidence is Confidence.LOW:
                raise SessionError("ALREADY_LOW", f"{receipt_id} is already low confidence", 409)
            n = self.plan.version + 1
            return self._replan(f"Plan v{n}: receipt confidence lowered",
                                [f"Receipt {r.receipt_id} ({r.payer}, {r.currency} {m(r.amount)}): confidence HIGH to LOW"],
                                self.plan.inputs.with_receipt_confidence(receipt_id, Confidence.LOW))

    def simulate_deposit(self, currency: str, amount) -> dict:
        with self._lock:
            if currency not in self.plan.inputs.balances:
                raise SessionError("UNKNOWN_CURRENCY", f"no {currency} balance", 400)
            try:
                amt = Decimal(str(amount))
            except (InvalidOperation, ValueError):
                raise SessionError("BAD_AMOUNT", "amount must be a number", 400) from None
            if not amt.is_finite() or amt <= 0 or amt > Decimal("100000"):
                raise SessionError("BAD_AMOUNT", "amount must be above 0 and at most 100,000", 400)
            try:
                dep = self.calls.simulate_deposit(request_id=f"dep-{uuid.uuid4().hex}", currency=currency, amount=amt)
            except (AirwallexError, UncertainOutcome) as e:
                raise SessionError("DEPOSIT_FAILED", str(e), 502) from None
            try:
                new = refresh_balances(self.calls, self.plan.inputs)
            except (AirwallexError, UncertainOutcome):
                raise SessionError("DEPOSIT_DONE_BALANCES_UNREAD",
                                   f"The deposit of {dep.currency} {m(dep.amount)} was simulated, but balances could "
                                   "not be read again, so the plan was not updated. Do not simulate it again.",
                                   502) from None
            self._balances_seen = (dict(new.balances), self.clock.now())
            n = self.plan.version + 1
            return self._replan(f"Plan v{n}: deposit simulated",
                                [f"Simulated deposit: {dep.currency} +{m(dep.amount)} (deposit simulation endpoint, {self._src()})"],
                                new)

    # ---- approval requests ----------------------------------------------------------------------------
    def _ob(self, oid: str) -> Obligation:
        for o in self.plan.inputs.obligations:
            if o.obligation_id == oid:
                return o
        raise SessionError("UNKNOWN_OBLIGATION", f"no obligation {oid}", 404)

    def _ex(self, eid: str):
        ex = self.executor.executions.get(eid)
        if ex is None:
            raise SessionError("UNKNOWN_EXECUTION", f"no request {eid}", 404)
        return ex

    def _handed_out(self, prep) -> dict:
        eid = prep.execution.execution_id
        self.tokens[eid] = prep.token
        return {"execution_id": eid, "token": prep.token, "approver_path": f"/approve/{prep.token}"}

    def prepare(self, obligation_id: str, validity: str = "MIN_1") -> dict:
        if validity not in VALIDITY_SECONDS:
            raise SessionError("BAD_VALIDITY", f"quote validity must be one of {sorted(VALIDITY_SECONDS)}", 400)
        with self._lock:
            ob = self._ob(obligation_id)
            d = self.plan.decision(obligation_id)
            try:
                prep = self.executor.prepare(d, ob, self.plan.digest, validity=validity)
            except EnvelopeError as e:
                raise SessionError(e.code, str(e), 409) from None
            except StateError as e:
                raise SessionError("LIVE_REQUEST_EXISTS", str(e), 409) from None
            except (AirwallexError, UncertainOutcome) as e:
                raise SessionError("QUOTE_FAILED", str(e), 502) from None
            return self._handed_out(prep)

    def requote(self, execution_id: str) -> dict:
        with self._lock:
            self._ex(execution_id)
            try:
                prep, diff = self.executor.requote(execution_id)
            except StateError as e:
                raise SessionError("CANNOT_REQUOTE", str(e), 409) from None
            except (AirwallexError, UncertainOutcome) as e:
                raise SessionError("QUOTE_FAILED", str(e), 502) from None
            out = self._handed_out(prep)
            self.requotes[execution_id] = {
                "new_execution": out["execution_id"], "currency": diff.currency, "old_sell": m(diff.old_sell),
                "new_sell": m(diff.new_sell), "delta": m(abs(diff.delta)), "direction": diff.direction,
                "text": diff.text}
            out["difference"] = self.requotes[execution_id]
            return out

    # ---- the approver page -------------------------------------------------------------------------------
    def _peek(self, token: str):
        try:
            env = self.gate.peek(token)
        except ApprovalError:
            raise SessionError("UNKNOWN_TOKEN", MESSAGES["UNKNOWN_TOKEN"], 404) from None
        return env, self.executor.executions[env.envelope_id]

    def approver_view(self, token: str) -> dict:
        with self._lock:
            env, ex = self._peek(token)
            now = self.clock.now()
            q = env.quote
            end = q.valid_to_at if q is not None else env.expires_at
            if ex.approval is not None:
                state, msg = "used", MESSAGES["NONCE_USED"]
            elif ex.state is State.DENIED:
                state, msg = "denied", MESSAGES["DENIED"]
            elif ex.state is State.VOIDED:
                state, msg = "terms_changed", MESSAGES["TERMS_CHANGED"]
            elif ex.state is State.EXPIRED or now > end:
                state = "expired"
                if q is None:  # a FUND request has no quote and no rate: only its approval window
                    msg = f"The approval window closed at {env.expires_at:%H:%M:%S} UTC. Nothing was paid."
                else:
                    msg = (f"This rate expired at {end:%H:%M:%S} UTC. Nothing was converted." if now > end else
                           f"The approval window closed at {env.expires_at:%H:%M:%S} UTC, before the rate ended. "
                           "Nothing was converted.")
            elif now >= env.expires_at:
                state, msg = "too_close", MESSAGES["TOO_CLOSE_TO_EXPIRY"]
            else:
                state, msg = "live", "Approve or deny these exact terms. Approval works once."
            if env.mode is Action.FUND:
                you_pay = {"currency": env.payout_currency, "amount": m(env.payout_amount + env.payout_fee),
                           "note": f"from the {env.payout_currency} balance"}
            else:
                you_pay = {"currency": q.sell_currency, "amount": m(q.sell_amount), "note": "converted at this quote"}
            return {
                "state": state, "message": msg, "server_now": _iso(now), "world": self.world,
                "chips": self._chips(), "in_words": in_words(env),
                "mode": env.mode.value, "supplier": env.supplier_name, "invoice_ref": env.invoice_ref,
                "you_pay": you_pay,
                "supplier_gets": {"currency": env.payout_currency, "amount": m(env.payout_amount)},
                "from_balance": m(env.existing_balance_used) if env.mode is Action.CONVERT else None,
                "fee": m(env.payout_fee), "payout_method": env.payout_method,
                "rate": None if q is None else format(q.client_rate, "f"),
                "pair": None if q is None else f"{q.buy_currency}/{q.sell_currency}",
                "quote_id": None if q is None else q.quote_id,
                "valid_from_at": _iso(q.valid_from_at if q else env.created_at),
                "valid_to_at": _iso(end), "expires_at": _iso(env.expires_at),
                "dispatch_margin_s": int(self.gate.dispatch_margin.total_seconds()),
                "digest": env.digest, "digest_prefix_len": DIGEST_PREFIX,
            }

    def approver_approve(self, token: str, digest: str, typed: str) -> dict:
        with self._lock:
            self._peek(token)
            digest = str(digest or "")
            if not typed or str(typed).strip().lower() != digest[:DIGEST_PREFIX].lower() or len(digest) < DIGEST_PREFIX:
                raise SessionError("DIGEST_PREFIX", f"Type the first {DIGEST_PREFIX} characters of the digest shown "
                                   "on this page.", 400)
            out = self.executor.approve(token, digest, APPROVER_ID)
            return {"ok": out.ok, "code": out.code, "message": out.message, "view": self.approver_view(token)}

    def approver_deny(self, token: str) -> dict:
        with self._lock:
            self._peek(token)
            try:
                self.executor.deny(token, APPROVER_ID)
            except ApprovalError as e:
                raise SessionError(e.code, e.message, 409) from None
            except StateError as e:
                raise SessionError("CANNOT_DENY", str(e), 409) from None
            return {"ok": True, "code": "DENIED", "message": MESSAGES["DENIED"], "view": self.approver_view(token)}

    # ---- execution -----------------------------------------------------------------------------------------
    def dispatch(self, execution_id: str) -> dict:
        with self._lock:
            self._ex(execution_id)
            self.executor.dispatch(execution_id)
            self._refresh_balances()
            return self.execution(execution_id)

    def advance(self, execution_id: str) -> dict:
        with self._lock:
            self._ex(execution_id)
            self.executor.advance(execution_id)
            self._refresh_balances()
            return self.execution(execution_id)

    def reconcile(self, execution_id: str) -> dict:
        with self._lock:
            self._ex(execution_id)
            self.executor.reconcile(execution_id)
            self._refresh_balances()
            return self.execution(execution_id)

    def simulate_transition(self, execution_id: str, status: str) -> dict:
        """POST /simulation/transfers/{id}/transition: a sandbox simulation endpoint (the fake models it)."""
        with self._lock:
            ex = self._ex(execution_id)
            if status not in TRANSITIONS:
                raise SessionError("BAD_TRANSITION", f"status must be one of {TRANSITIONS}", 400)
            if ex.transfer is None:
                raise SessionError("NO_TRANSFER", "there is no supplier payment to move yet", 409)
            try:
                if self.world == "SANDBOX" and status == "PAID":
                    # Builder guide: "Simulate SENT before PAID." (confirmed in the sandbox run of 2026-11-05: SENT, then PAID)
                    self.calls.simulate_transfer_transition(ex.transfer.transfer_id, "SENT")
                self.calls.simulate_transfer_transition(ex.transfer.transfer_id, status)
            except (AirwallexError, UncertainOutcome) as e:
                raise SessionError("TRANSITION_FAILED", str(e), 409) from None
            self._log_control("transfer_transition", f"Transfer simulation endpoint moved {ex.transfer.transfer_id} "
                              f"to {status} ({self._src()})")
            self.executor.advance(execution_id)
            self._refresh_balances()
            return self.execution(execution_id)

    def record(self, execution_id: str) -> dict:
        with self._lock:
            self._ex(execution_id)
            return self.executor.record(execution_id).to_dict()

    @staticmethod
    def verify(bundle, expected_digest: Optional[str] = None) -> dict:
        try:
            problems = verify_record(bundle, expected_envelope_digest=expected_digest)
        except Exception as e:  # noqa: BLE001 - any unreadable copy is a FAIL, never a crash
            problems = [f"UNREADABLE_BUNDLE {type(e).__name__}"]
        return {"verdict": "PASS" if not problems else "FAIL", "problems": problems}

    # ---- FAKE-world test controls ------------------------------------------------------------------------
    def _need_fake(self):
        if self._fake is None:
            raise SessionError("FAKE_ONLY", "test controls exist only in the FAKE world", 403)
        return self._fake

    def _log_control(self, control: str, text: str) -> None:
        self.controls_log.append({"control": control, "text": text, "at": _iso(self.clock.now())})

    def skip_clock(self, seconds) -> dict:
        with self._lock:
            self._need_fake()
            try:
                s = int(seconds)
            except (TypeError, ValueError):
                raise SessionError("BAD_SECONDS", "seconds must be a whole number", 400) from None
            if not 1 <= s <= 600:
                raise SessionError("BAD_SECONDS", "seconds must be between 1 and 600", 400)
            self.clock.advance(s)
            self._log_control("skip_clock", f"Fake clock moved forward {s} s (time cut, FAKE world only)")
            return {"server_now": _iso(self.clock.now())}

    def set_fake_rate(self, pair: str, rate) -> dict:
        with self._lock:
            fake = self._need_fake()
            if not re.fullmatch(r"[A-Z]{6}", str(pair)):
                raise SessionError("BAD_PAIR", "pair must look like GBPUSD", 400)
            try:
                r = Decimal(str(rate))
            except (InvalidOperation, ValueError):
                raise SessionError("BAD_RATE", "rate must be a number", 400) from None
            if not r.is_finite() or r <= 0 or r > 1000:
                raise SessionError("BAD_RATE", "rate must be above 0", 400)
            fake.set_rate(pair, r)
            self._log_control("set_fake_rate", f"Fake market rate {pair} set to {r} (FAKE world only)")
            return {"pair": pair, "rate": str(r)}

    def withhold_next_response(self, op: str) -> dict:
        with self._lock:
            fake = self._need_fake()
            if op not in WITHHOLDABLE:
                raise SessionError("BAD_OP", f"op must be one of {WITHHOLDABLE}", 400)
            fake.inject(op, "drop_response")
            self._log_control("withhold_next_response",
                              f"Response to the next {op} withheld by our test switch (FAKE world only)")
            return {"op": op}

    # ---- views ------------------------------------------------------------------------------------------------
    def _decision_json(self, plan: Plan, d) -> dict:
        ob = next(o for o in plan.inputs.obligations if o.obligation_id == d.obligation_id)
        return {
            "obligation_id": d.obligation_id, "supplier": ob.supplier_name, "invoice_ref": ob.invoice_ref,
            "amount": m(ob.amount), "currency": ob.currency, "due_at": _iso(ob.due_at),
            "action": d.action.value, "reason_code": d.reason_code, "reason_text": d.reason_text,
            "approval": _approval(d), "uses": _uses(plan, d),
            "executable": d.action in (Action.FUND, Action.CONVERT),
            "ceiling_level": None if d.ceiling_level is None else d.ceiling_level.value,
            "ceiling_amount": None if d.ceiling_amount is None else m(d.ceiling_amount),
            "est_sell": None if d.est_sell_amount is None else m(d.est_sell_amount),
        }

    def _plan_json(self, st: Stage) -> dict:
        p = st.plan
        r = st.result
        return {
            "version": p.version, "title": st.title, "trigger": st.trigger, "diff": st.diff,
            "digest": p.digest, "ceiling": p.ceiling.value,
            "ceiling_amount": f"{p.inputs.policy.base_currency} {m(_cap(p.inputs.policy, p.ceiling))}",
            "recomputed": list(r.recomputed) if r else [d.obligation_id for d in p.decisions],
            "reused": list(r.reused) if r else [], "changed": list(r.changed) if r else [],
            "decisions": [self._decision_json(p, d) for d in p.decisions],
            "explanation": st.explanation,
        }

    def execution(self, execution_id: str) -> dict:
        with self._lock:
            ex = self._ex(execution_id)
            env = ex.envelope
            q = env.quote
            tok = self.tokens.get(execution_id)
            refusal = next((e.data.get("code") for e in reversed(ex.events) if e.kind == "approval_refused"), None)
            return {
                "execution_id": ex.execution_id, "obligation_id": ex.obligation.obligation_id,
                "supplier": env.supplier_name, "invoice_ref": env.invoice_ref, "mode": env.mode.value,
                "state": ex.state.value, "state_text": ex.text, "digest": env.digest, "in_words": in_words(env),
                "approval_margin_s": int(self.gate.dispatch_margin.total_seconds()),
                "created_at": _iso(env.created_at), "expires_at": _iso(env.expires_at),
                "quote": None if q is None else {
                    "quote_id": q.quote_id, "sell_currency": q.sell_currency, "buy_currency": q.buy_currency,
                    "sell_amount": m(q.sell_amount), "buy_amount": m(q.buy_amount),
                    "client_rate": format(q.client_rate, "f"), "valid_from_at": _iso(q.valid_from_at),
                    "valid_to_at": _iso(q.valid_to_at)},
                "payout": {"currency": env.payout_currency, "amount": m(env.payout_amount), "fee": m(env.payout_fee),
                           "method": env.payout_method, "from_balance": m(env.existing_balance_used)},
                "conversion_request_id": env.conversion_request_id, "transfer_request_id": env.transfer_request_id,
                "approver_path": f"/approve/{tok}" if tok else None,
                "counts": self.executor.call_counts(execution_id),
                "supersedes": ex.supersedes, "superseded_by": ex.superseded_by,
                "requote": self.requotes.get(execution_id), "refusal": refusal,
                "conversion": None if ex.conversion is None else {
                    "conversion_id": ex.conversion.conversion_id, "status": ex.conversion.status,
                    "quote_id": ex.conversion.quote_id},
                "transfer": None if ex.transfer is None else {
                    "transfer_id": ex.transfer.transfer_id, "status": ex.transfer.status},
                "events": [{"seq": e.seq, "at": _iso(e.at), "kind": e.kind,
                            "data": {k: _plain(v) for k, v in e.data.items()}} for e in ex.events],
            }

    def _reserve(self) -> dict:
        bal, read_at = self._balances_seen
        moved = {ex.obligation.obligation_id for ex in self.executor.executions.values()
                 if ex.state in MONEY_MAY_HAVE_MOVED}
        reserved: dict = {}
        for d in self.plan.decisions:
            if d.obligation_id in moved:
                continue
            for key, amt in d.reserves:
                if key.startswith("balance:"):
                    c = key.split(":", 1)[1]
                    reserved[c] = reserved.get(c, Decimal("0")) + amt
        rows = [{"currency": c, "available": m(a), "reserved": m(reserved.get(c, Decimal("0"))),
                 "remaining": m(a - reserved.get(c, Decimal("0")))} for c, a in sorted(bal.items())]
        return {"rows": rows, "read_at": _iso(read_at), "plan_version": self.plan.version,
                "excluded": sorted(moved),
                "note": "Computed by this app: provider balance minus what the current plan relies on for "
                        "obligations not yet executed. Demo policy, not an Airwallex hold."}

    def _chips(self) -> dict:
        return {"world": "FAKE" if self.world == "FAKE" else "AIRWALLEX SANDBOX", "money": "Test money",
                "run": f"LIVE {self.run_id} {self.commit}", "reader": self.reader.label}

    def _invoice_json(self, inv, ex) -> dict:
        review = self.reviews.get(inv.invoice_id)
        resolutions = review["resolutions"] if review else {}

        def shown(name, value):
            if value is None:
                return None
            return _iso(value) if name == "due_date" else (m(value) if name == "amount" else str(value))

        fields = []
        for f in ex.fields:
            r = resolutions.get(f.name)
            fields.append({
                "name": f.name, "value": shown(f.name, r["parsed"] if r else f.value), "raw": f.raw,
                "quote": f.quote, "start": f.start, "end": f.end,
                "status": "RESOLVED" if r else f.status, "flags": list(f.flags),
                "local": f.value.strftime("%Y-%m-%dT%H:%M") if f.name == "due_date" and f.value is not None else None,
                "resolution": None if r is None else {k: r[k] for k in ("value", "entered", "note", "by", "at",
                                                                         "flags_before")}})
        return {"invoice_id": inv.invoice_id, "obligation_id": inv.obligation_id, "text": inv.text,
                "ready": ex.ready or (review is not None and review["status"] == "RESOLVED"),
                "flags": list(ex.flags), "reader": ex.reader,
                "review": {"status": None if review is None else review["status"]}, "fields": fields}

    def state(self) -> dict:
        with self._lock:
            self.executor.lapse_overdue()
            src = self._src()
            bal, read_at = self._balances_seen
            p = self.stages[-1].plan if self.stages else None
            inputs = p.inputs if p is not None else self._seed
            return {
                "world": self.world,
                "chips": self._chips(),
                "sources": {"balances": src, "rates": src, "quotes": src, "calls": src, "receipts": "FIXTURE",
                            "invoices": "FIXTURE", "reader": "MODEL" if self.reader.is_model else "TEST DOUBLE",
                            "plan": "CODE", "record": "CODE", "reserve": "CODE", "review": "HUMAN"},
                "controls": self._fake is not None,
                "server_now": _iso(self.clock.now()), "started_at": _iso(self.started_at),
                "run": {"run_id": self.run_id, "commit": self.commit, "world": self.world},
                "invoices": [self._invoice_json(inv, ex) for inv, ex in self.invoices],
                "review": {"pending": self._pending(),
                           "resolved": [{k: v for k, v in r.items() if k != "trigger"} for r in self.resolved]},
                "obligations": [{"obligation_id": o.obligation_id, "supplier_name": o.supplier_name,
                                 "amount": f"{o.amount:.2f}", "currency": o.currency, "due_at": _iso(o.due_at),
                                 "invoice_ref": o.invoice_ref, "beneficiary_id": o.beneficiary_id}
                                for o in self.obligations],
                "receipts": [{"receipt_id": r.receipt_id, "payer": r.payer, "amount": m(r.amount),
                              "currency": r.currency, "expected_at": _iso(r.expected_at),
                              "confidence": r.confidence.value} for r in inputs.receipts],
                "balances": {"rows": [{"currency": c, "available": m(a)} for c, a in sorted(bal.items())],
                             "read_at": _iso(read_at), "note": getattr(self.calls, "float_note", None)},
                "rates": [] if p is None else [{"pair": f"{s}/{b}", "sell_per_buy": format(u, "f")}
                                               for (s, b), u in sorted(p.inputs.rates.sell_per_buy.items())],
                "policy": {"base": inputs.policy.base_currency, "planning_now": _iso(inputs.now),
                           "planning_pinned": self._plan_now is not None,
                           "ceiling_high": m(inputs.policy.ceiling_high), "ceiling_low": m(inputs.policy.ceiling_low),
                           "horizon_hours": int(inputs.policy.horizon.total_seconds() // 3600),
                           "buffer_bps": inputs.policy.rate_buffer_bps,
                           "label": "Demo policy, not an Airwallex setting"},
                "plans": [self._plan_json(st) for st in self.stages],
                "explanation": self.stages[-1].explanation if self.stages else None,
                "executions": [self.execution(eid) for eid in self.executor.executions],
                "reserve": self._reserve() if self.stages else None,
                "calls": [{"seq": c.seq, "op": c.op, "request_id": c.request_id, "outcome": c.outcome}
                          for c in self.calls.log],
                "controls_log": list(self.controls_log),
            }

"""Deterministic planner: code decides, per obligation, FUND / CONVERT / DEFER / ESCALATE.

No model output reaches this module. Inputs are plain values; the same inputs give the same plan.

Priority: obligations are served in due-date order (ties by id). Each decision reserves the cash it
relies on from shared pools (balance:<CCY> and receipt:<ID>) so two obligations cannot spend the same money.

Order of checks for one obligation (first match wins):
  1. Not due inside the planning horizon          -> DEFER (NOT_DUE_IN_HORIZON)
  2. Existing balance in the payout currency covers need -> FUND
  3. Convert the shortfall from the base currency, if the (buffered) estimate is covered by the
     base balance AND is within the forecast-confidence ceiling -> CONVERT (a person still approves the exact quote)
  4. A HIGH-confidence expected receipt in the payout currency covers the shortfall and arrives at least
     `receipt_lead` before the due date -> DEFER (wait for the receipt)
  5. Otherwise -> ESCALATE (CEILING_EXCEEDED, CASH_SHORT or NO_RATE)

The forecast-confidence ceiling only decides ESCALATE versus NEEDS_APPROVAL for a conversion. It never
removes the human approval of the exact quote. Ceiling amounts are demo policy constants.

`replan` recomputes only the decisions whose recorded reads touch what changed, reuses the others
unchanged, and is tested to equal a full recompute.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import ROUND_UP, Decimal
from types import MappingProxyType
from typing import Mapping, Optional

from .models import Action, ApprovalNeed, Confidence, Obligation, Receipt

CENT = Decimal("0.01")


def fmt(amount: Decimal, currency: str) -> str:
    return f"{currency} {amount:,.2f}"


@dataclass(frozen=True)
class Policy:
    """Demo policy constants (labelled as such in the UI). Not an Airwallex setting."""

    base_currency: str = "USD"
    horizon: timedelta = timedelta(hours=72)
    receipt_lead: timedelta = timedelta(hours=12)
    ceiling_high: Decimal = Decimal("5000")  # base-currency cost an agent may prepare when forecasts are trusted
    ceiling_low: Decimal = Decimal("2000")   # otherwise
    rate_buffer_bps: int = 50                # 0.5% cushion on the indicative estimate


@dataclass(frozen=True)
class RateBook:
    """Indicative cost of one unit of the buy currency, in the sell currency, keyed (sell, buy)."""

    sell_per_buy: Mapping

    def __post_init__(self):
        # snapshot: later changes to the caller's dict must not alter a plan's inputs
        object.__setattr__(self, "sell_per_buy", MappingProxyType(dict(self.sell_per_buy)))

    def cost(self, sell: str, buy: str, buy_amount: Decimal, buffer_bps: int) -> Decimal:
        unit = self.sell_per_buy[(sell, buy)]
        raw = buy_amount * unit * (Decimal(10000 + buffer_bps) / Decimal(10000))
        return raw.quantize(CENT, rounding=ROUND_UP)

    def has(self, sell: str, buy: str) -> bool:
        return (sell, buy) in self.sell_per_buy


@dataclass(frozen=True)
class PlanInputs:
    now: datetime
    obligations: tuple
    balances: Mapping
    receipts: tuple
    rates: RateBook
    policy: Policy

    def __post_init__(self):
        ids = [o.obligation_id for o in self.obligations]
        if len(ids) != len(set(ids)):
            raise ValueError("obligation ids must be unique")
        rids = [r.receipt_id for r in self.receipts]
        if len(rids) != len(set(rids)):
            raise ValueError("receipt ids must be unique")
        object.__setattr__(self, "obligations", tuple(self.obligations))
        object.__setattr__(self, "receipts", tuple(self.receipts))
        object.__setattr__(self, "balances", MappingProxyType({c: Decimal(a) for c, a in self.balances.items()}))

    def with_deposit(self, currency: str, amount: Decimal) -> "PlanInputs":
        bal = dict(self.balances)
        bal[currency] = bal.get(currency, Decimal("0")) + amount
        return replace(self, balances=bal)

    def with_receipt_confidence(self, receipt_id: str, confidence: Confidence) -> "PlanInputs":
        found = [r for r in self.receipts if r.receipt_id == receipt_id]
        if not found:
            raise KeyError(receipt_id)
        return replace(self, receipts=tuple(
            replace(r, confidence=confidence) if r.receipt_id == receipt_id else r for r in self.receipts))


@dataclass(frozen=True)
class Decision:
    obligation_id: str
    action: Action
    reason_code: str
    reason_text: str
    approval: ApprovalNeed
    pay_amount: Decimal                     # payout amount plus payout fee, in the payout currency
    payout_currency: str
    from_balance: Decimal = Decimal("0")    # taken from the existing payout-currency balance
    convert_currency: Optional[str] = None  # buy side
    convert_buy_amount: Optional[Decimal] = None
    sell_currency: Optional[str] = None
    est_sell_amount: Optional[Decimal] = None
    receipt_id: Optional[str] = None
    ceiling_level: Optional[Confidence] = None
    ceiling_amount: Optional[Decimal] = None
    reads: frozenset = frozenset()
    reserves: tuple = ()                    # sorted ((pool_key, amount), ...)

    def outcome(self) -> tuple:
        return (self.action, self.reason_code, self.from_balance, self.convert_buy_amount,
                self.est_sell_amount, self.receipt_id)


@dataclass(frozen=True)
class Plan:
    inputs: PlanInputs
    decisions: tuple
    version: int
    ceiling: Confidence
    digest: str

    def decision(self, obligation_id: str) -> Decision:
        for d in self.decisions:
            if d.obligation_id == obligation_id:
                return d
        raise KeyError(obligation_id)


@dataclass(frozen=True)
class ReplanResult:
    plan: Plan
    recomputed: tuple
    reused: tuple
    changed: tuple
    touched: tuple


# ---- helpers ---------------------------------------------------------------------------------

def ordered(obligations) -> list:
    return sorted(obligations, key=lambda o: (o.due_at, o.obligation_id))


def ceiling_level(inp: PlanInputs) -> Confidence:
    """HIGH only if at least one receipt is expected inside the horizon and every such receipt is HIGH."""
    end = inp.now + inp.policy.horizon
    inside = [r for r in inp.receipts if inp.now <= r.expected_at <= end]
    if inside and all(r.confidence is Confidence.HIGH for r in inside):
        return Confidence.HIGH
    return Confidence.LOW


def _initial_pools(inp: PlanInputs) -> dict:
    pools = {f"balance:{c}": Decimal(a) for c, a in inp.balances.items()}
    for r in inp.receipts:
        pools[f"receipt:{r.receipt_id}"] = r.amount
    return pools


def _apply(pools: dict, reserves: tuple) -> None:
    for key, amount in reserves:
        pools[key] = pools.get(key, Decimal("0")) - amount


def _decision_digest_payload(d: Decision) -> dict:
    return {
        "id": d.obligation_id, "action": d.action.value, "reason": d.reason_code,
        "from_balance": str(d.from_balance),
        "buy": None if d.convert_buy_amount is None else str(d.convert_buy_amount),
        "est_sell": None if d.est_sell_amount is None else str(d.est_sell_amount),
        "receipt": d.receipt_id,
    }


def _digest(inp: PlanInputs, decisions: tuple) -> str:
    payload = {"now": inp.now.isoformat(), "decisions": [_decision_digest_payload(d) for d in decisions]}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _approval_for(action: Action) -> ApprovalNeed:
    if action in (Action.FUND, Action.CONVERT):
        return ApprovalNeed.NEEDS_APPROVAL
    if action is Action.ESCALATE:
        return ApprovalNeed.ESCALATED
    return ApprovalNeed.NOT_APPLICABLE


# ---- the decision for one obligation ----------------------------------------------------------

def _decide(ob: Obligation, pools: dict, inp: PlanInputs, level: Confidence) -> Decision:
    pol = inp.policy
    reads = {f"obligation:{ob.obligation_id}"}
    cur = ob.currency
    need = ob.total_needed

    def make(action, code, text, **kw):
        reserves = tuple(sorted(kw.pop("reserves", {}).items()))
        return Decision(ob.obligation_id, action, code, text, _approval_for(action), need, cur,
                        reads=frozenset(reads), reserves=reserves, **kw)

    if ob.due_at > inp.now + pol.horizon:
        return make(Action.DEFER, "NOT_DUE_IN_HORIZON",
                    f"Due {ob.due_at:%Y-%m-%d %H:%M} UTC, after the {int(pol.horizon.total_seconds() // 3600)} hour planning window.")

    reads.add(f"balance:{cur}")
    avail = max(pools.get(f"balance:{cur}", Decimal("0")), Decimal("0"))
    if avail >= need:
        return make(Action.FUND, "BALANCE_COVERS",
                    f"The {cur} balance covers {fmt(need, cur)}.",
                    from_balance=need, reserves={f"balance:{cur}": need})

    shortfall = need - avail
    base = pol.base_currency
    cost = None
    no_rate = False
    base_avail = Decimal("0")
    can_convert = cur != base
    if can_convert:
        reads.update({f"balance:{base}", f"rate:{base}>{cur}"})
        if not inp.rates.has(base, cur):
            no_rate = True  # cannot price a conversion; a confident receipt can still cover the shortfall
            can_convert = False
        else:
            cost = inp.rates.cost(base, cur, shortfall, pol.rate_buffer_bps)
            base_avail = max(pools.get(f"balance:{base}", Decimal("0")), Decimal("0"))

    ceiling_cap = None
    if can_convert and cost <= base_avail:
        reads.add("policy:ceiling")
        ceiling_cap = pol.ceiling_high if level is Confidence.HIGH else pol.ceiling_low
        if cost <= ceiling_cap:
            reserves = {f"balance:{base}": cost}
            if avail > 0:
                reserves[f"balance:{cur}"] = avail
            return make(Action.CONVERT, "CONVERT_AT_QUOTE",
                        f"{fmt(shortfall, cur)} short. Buy {cur} at a locked quote, estimated {fmt(cost, base)} "
                        f"including a {pol.rate_buffer_bps / 100:.1f}% buffer.",
                        from_balance=avail, convert_currency=cur, convert_buy_amount=shortfall,
                        sell_currency=base, est_sell_amount=cost, ceiling_level=level,
                        ceiling_amount=ceiling_cap, reserves=reserves)

    # Receipt route: wait for a confident receipt in the payout currency.
    reads.add(f"receipts:{cur}")
    candidates = sorted((r for r in inp.receipts if r.currency == cur), key=lambda r: (r.expected_at, r.receipt_id))
    low_seen = False
    for r in candidates:
        reads.add(f"receipt:{r.receipt_id}")
        if r.confidence is not Confidence.HIGH:
            low_seen = True
            continue
        remaining = pools.get(f"receipt:{r.receipt_id}", r.amount)
        if (remaining >= shortfall and r.expected_at > inp.now
                and r.expected_at <= ob.due_at - pol.receipt_lead):
            reserves = {f"receipt:{r.receipt_id}": shortfall}
            if avail > 0:
                reserves[f"balance:{cur}"] = avail
            return make(Action.DEFER, "WAIT_FOR_RECEIPT",
                        f"{fmt(shortfall, cur)} short. {r.payer} is expected to pay {fmt(r.amount, cur)} on "
                        f"{r.expected_at:%Y-%m-%d %H:%M} UTC (high confidence), before the due date.",
                        from_balance=avail, receipt_id=r.receipt_id, reserves=reserves)

    if ceiling_cap is not None:
        return make(Action.ESCALATE, "CEILING_EXCEEDED",
                    f"Estimated cost {fmt(cost, base)} is above the {level.value.lower()}-confidence limit of "
                    f"{fmt(ceiling_cap, base)}. A person decides.",
                    ceiling_level=level, ceiling_amount=ceiling_cap)
    if no_rate:
        return make(Action.ESCALATE, "NO_RATE",
                    f"No indicative {base}/{cur} rate is available, so the cost cannot be estimated.")
    why = " Only a low-confidence receipt is expected." if low_seen else ""
    return make(Action.ESCALATE, "CASH_SHORT",
                f"{fmt(shortfall, cur)} short and no confident funding source.{why} A person decides.")


# ---- public API ---------------------------------------------------------------------------------

def make_plan(inp: PlanInputs, *, version: int = 1) -> Plan:
    level = ceiling_level(inp)
    pools = _initial_pools(inp)
    decisions = []
    for ob in ordered(inp.obligations):
        d = _decide(ob, pools, inp, level)
        _apply(pools, d.reserves)
        decisions.append(d)
    decisions = tuple(decisions)
    return Plan(inp, decisions, version, level, _digest(inp, decisions))


def _diff_touched(old: PlanInputs, new: PlanInputs) -> set:
    touched = set()
    for c in set(old.balances) | set(new.balances):
        if old.balances.get(c, Decimal("0")) != new.balances.get(c, Decimal("0")):
            touched.add(f"balance:{c}")
    old_r = {r.receipt_id: r for r in old.receipts}
    new_r = {r.receipt_id: r for r in new.receipts}
    for rid in set(old_r) | set(new_r):
        if old_r.get(rid) != new_r.get(rid):
            touched.add(f"receipt:{rid}")
            for r in (old_r.get(rid), new_r.get(rid)):
                if r is not None:
                    touched.add(f"receipts:{r.currency}")
    for pair in set(old.rates.sell_per_buy) | set(new.rates.sell_per_buy):
        if old.rates.sell_per_buy.get(pair) != new.rates.sell_per_buy.get(pair):
            touched.add(f"rate:{pair[0]}>{pair[1]}")
    if ceiling_level(old) != ceiling_level(new):
        touched.add("policy:ceiling")
    return touched


def replan(prev: Plan, new: PlanInputs) -> ReplanResult:
    old = prev.inputs
    everything = (new.now != old.now or new.policy != old.policy or new.obligations != old.obligations)
    touched = set() if everything else _diff_touched(old, new)
    level = ceiling_level(new)
    pools = _initial_pools(new)
    prev_by_id = {d.obligation_id: d for d in prev.decisions}
    out, recomputed, reused, changed = [], [], [], []
    for ob in ordered(new.obligations):
        pd = prev_by_id.get(ob.obligation_id)
        if pd is not None and not everything and not (pd.reads & touched):
            d = pd
            reused.append(ob.obligation_id)
        else:
            d = _decide(ob, pools, new, level)
            recomputed.append(ob.obligation_id)
            if pd is None or pd.outcome() != d.outcome():
                changed.append(ob.obligation_id)
            if pd is not None:
                old_res, new_res = dict(pd.reserves), dict(d.reserves)
                for key in set(old_res) | set(new_res):
                    if old_res.get(key) != new_res.get(key):
                        touched.add(key)
        _apply(pools, d.reserves)
        out.append(d)
    decisions = tuple(out)
    plan = Plan(new, decisions, prev.version + 1, level, _digest(new, decisions))
    return ReplanResult(plan, tuple(recomputed), tuple(reused), tuple(changed), tuple(sorted(touched)))

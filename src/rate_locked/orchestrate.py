"""Glue between the adapter and the planner: read balances and indicative rates, build PlanInputs.

Read-only calls plus the sandbox deposit simulation. No financial call lives here.
"""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import Iterable

from .awx.adapter import AirwallexError
from .planner import PlanInputs, Policy, RateBook

REFERENCE_BUY = Decimal("1000")  # indicative unit cost is read for this buy amount, then divided back to one unit


def read_balances(adapter) -> dict:
    return {b.currency: b.available for b in adapter.get_balances()}


def read_rates(adapter, base: str, currencies: Iterable[str]) -> RateBook:
    """GET /fx/rates/current for each payout currency. A pair the provider cannot price is left out (planner: NO_RATE)."""
    pairs = {}
    for c in sorted(set(currencies) - {base}):
        try:
            est = adapter.get_current_rate(sell_currency=base, buy_currency=c, buy_amount=REFERENCE_BUY)
        except AirwallexError:
            continue
        pairs[(base, c)] = est.sell_amount / est.buy_amount
    return RateBook(pairs)


def snapshot_inputs(adapter, obligations, receipts, policy: Policy, now) -> PlanInputs:
    obligations = tuple(obligations)
    return PlanInputs(
        now=now, obligations=obligations, balances=read_balances(adapter), receipts=tuple(receipts),
        rates=read_rates(adapter, policy.base_currency, (o.currency for o in obligations)), policy=policy)


def refresh_balances(adapter, inputs: PlanInputs) -> PlanInputs:
    return replace(inputs, balances=read_balances(adapter))

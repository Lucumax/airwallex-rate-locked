"""The demo scenario: three fictional obligations in three currencies. Test fixture, not data from any account."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from .models import Confidence, Obligation, Receipt
from .planner import PlanInputs, Policy, RateBook

D = Decimal
NOW = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)


def demo_inputs() -> PlanInputs:
    def h(n):
        return NOW + timedelta(hours=n)

    return PlanInputs(
        now=NOW,
        obligations=(
            Obligation("O1", "Acme Co.", "ben-acme", D("1200.00"), "EUR", h(20), invoice_ref="INV-1001"),
            Obligation("O2", "Globex Ltd.", "ben-globex", D("1000.00"), "GBP", h(30), invoice_ref="INV-2040"),
            Obligation("O3", "Initech Pte. Ltd.", "ben-initech", D("2000.00"), "EUR", h(60), invoice_ref="INV-3307"),
        ),
        balances={"USD": D("3000.00"), "EUR": D("1500.00"), "GBP": D("0.00")},
        receipts=(
            Receipt("R1", "Vandelay Industries", D("1800.00"), "EUR", h(40), Confidence.HIGH),
        ),
        rates=RateBook({("USD", "EUR"): D("1.08"), ("USD", "GBP"): D("1.30")}),
        policy=Policy(),
    )

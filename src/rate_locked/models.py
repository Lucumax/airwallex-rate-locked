"""Plain domain types shared by the planner, approval binding, state machine and record."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum


class Action(str, Enum):
    FUND = "FUND"          # pay from the existing same-currency balance
    CONVERT = "CONVERT"    # convert at an approved quote, then pay
    DEFER = "DEFER"        # do nothing now; a dated reason says why waiting is safe
    ESCALATE = "ESCALATE"  # a person decides; the agent prepares nothing to execute


class Confidence(str, Enum):
    HIGH = "HIGH"
    LOW = "LOW"


class ApprovalNeed(str, Enum):
    NEEDS_APPROVAL = "NEEDS_APPROVAL"  # FUND or CONVERT: a person approves the exact terms
    ESCALATED = "ESCALATED"            # above the ceiling or unfunded: a person decides first
    NOT_APPLICABLE = "NOT_APPLICABLE"  # DEFER: nothing to approve now


@dataclass(frozen=True)
class Obligation:
    obligation_id: str
    supplier_name: str          # fictional in the demo
    beneficiary_id: str
    amount: Decimal             # in `currency`
    currency: str
    due_at: datetime            # timezone-aware
    payout_fee: Decimal = Decimal("0")  # in `currency`; the guide says include a SWIFT fee in the conversion amount
    payout_method: str = "LOCAL"
    invoice_ref: str = ""

    def __post_init__(self):
        if self.amount <= 0:
            raise ValueError("obligation amount must be positive")
        if self.payout_fee < 0:
            raise ValueError("payout fee cannot be negative")
        if self.due_at.tzinfo is None:
            raise ValueError("due_at must be timezone-aware")

    @property
    def total_needed(self) -> Decimal:
        return self.amount + self.payout_fee


@dataclass(frozen=True)
class Receipt:
    """An expected incoming payment. Confidence is declared by the fixture (high or low)."""

    receipt_id: str
    payer: str
    amount: Decimal
    currency: str
    expected_at: datetime
    confidence: Confidence

    def __post_init__(self):
        if self.amount <= 0:
            raise ValueError("receipt amount must be positive")
        if self.expected_at.tzinfo is None:
            raise ValueError("expected_at must be timezone-aware")

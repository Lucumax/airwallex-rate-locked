"""Shared test setup for the money path."""
from dataclasses import replace
from decimal import Decimal as D

from rate_locked.approval import ApprovalGate
from rate_locked.awx.fake import FakeAirwallex
from rate_locked.clock import ManualClock
from rate_locked.executor import Executor
from rate_locked.planner import make_plan
from rate_locked.scenario import demo_inputs


# The one fixture invoice that goes to review (no time zone on its payment time), and the person's answer that
# the console flow records before plan v1 is built.
REVIEW = {"invoice_id": "inv-initech", "field": "due_date", "value": "2026-11-04T21:00:00+00:00",
          "note": "Checked with the supplier: times are UTC"}


def resolve_review(session, **overrides):
    r = {**REVIEW, **overrides}
    return session.resolve_review(r["invoice_id"], r["field"], r["value"], r["note"])


class World:
    """Fake provider + gate + executor on one manual clock, with the demo plan and a real fake beneficiary."""

    def __init__(self, **fake_kw):
        self.clock = ManualClock()
        self.fake = FakeAirwallex.demo(clock=self.clock, **fake_kw)
        self.gate = ApprovalGate(self.clock)
        self.executor = Executor(self.fake, self.gate, self.clock)
        self.ben = self.fake.create_beneficiary(request_id="ben-setup", payload={"name": "Fictional Supplier"})
        inp = demo_inputs()
        self.inputs = replace(inp, obligations=tuple(replace(o, beneficiary_id=self.ben.beneficiary_id)
                                                     for o in inp.obligations))
        self.plan = make_plan(self.inputs)

    def ob(self, oid):
        return next(o for o in self.inputs.obligations if o.obligation_id == oid)

    def prepare(self, oid="O2", validity="MIN_1"):
        return self.executor.prepare(self.plan.decision(oid), self.ob(oid), self.plan.digest, validity=validity)

    def approved(self, oid="O2", validity="MIN_1"):
        prep = self.prepare(oid, validity)
        out = self.executor.approve(prep.token, prep.execution.envelope.digest, "approver-1")
        assert out.ok, out
        return prep.execution

    def balances(self):
        return {b.currency: b.available for b in self.fake.get_balances()}

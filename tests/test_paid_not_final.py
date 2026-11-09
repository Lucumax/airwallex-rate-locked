"""PAID is the provider's report, not a final settlement: the lifecycle page says a PAID transfer can still end
FAILED. The executor keeps polling a PAID transfer, shows a late failure as PAYOUT_FAILED with its own events, never
sends a second payment, and never claims more than the provider said."""
from decimal import Decimal as D

from rate_locked.executor import State, TRANSITIONS

from helpers import World


def _paid(w):
    ex = w.approved("O2")
    ex = w.executor.dispatch(ex.execution_id)
    w.fake.simulate_transfer_transition(ex.transfer.transfer_id, "PAID")
    ex = w.executor.advance(ex.execution_id)
    assert ex.state is State.PAID
    return ex


def kinds(ex):
    return [e.kind for e in ex.events]


def test_paid_is_not_terminal_it_may_still_fail():
    assert State.PAYOUT_FAILED in TRANSITIONS[State.PAID]
    assert TRANSITIONS[State.PAID] == frozenset({State.PAYOUT_FAILED})


def test_paid_text_says_it_is_the_provider_report_and_can_still_fail():
    w = World()
    ex = _paid(w)
    assert "reported" in ex.text and "can still fail" in ex.text


def test_advance_on_a_paid_transfer_keeps_polling_and_stays_paid_when_nothing_changed():
    w = World()
    ex = _paid(w)
    ex = w.executor.advance(ex.execution_id)
    assert ex.state is State.PAID and ex.transfer.status == "PAID"
    assert "transfer_polled_after_paid" in kinds(ex)  # the poll really happened


def test_late_failure_after_paid_is_shown_not_hidden_and_needs_a_person():
    w = World()
    ex = _paid(w)
    w.fake.simulate_transfer_transition(ex.transfer.transfer_id, "FAILED")  # PAID can still fail late
    ex = w.executor.advance(ex.execution_id)
    assert ex.state is State.PAYOUT_FAILED
    assert ex.transfer.status == "FAILED"
    ks = kinds(ex)
    assert "late_failure_after_paid" in ks and "needs_person_after_paid" in ks
    assert ex.text.startswith("Payment failed")


def test_late_failure_never_sends_a_second_payment_or_conversion():
    w = World()
    ex = _paid(w)
    w.fake.simulate_transfer_transition(ex.transfer.transfer_id, "FAILED")
    ex = w.executor.advance(ex.execution_id)
    for _ in range(3):
        w.executor.advance(ex.execution_id)
        w.executor.dispatch(ex.execution_id)
        w.executor.reconcile(ex.execution_id)
    assert w.executor.call_counts(ex.execution_id) == {"conversion_create": 1, "transfer_create": 1}


def test_the_record_follows_the_late_failure_with_the_provider_status_verbatim():
    w = World()
    ex = _paid(w)
    w.fake.simulate_transfer_transition(ex.transfer.transfer_id, "FAILED")
    w.executor.advance(ex.execution_id)
    rec = w.executor.record(ex.execution_id)
    assert rec.state == "PAYOUT_FAILED"
    status = [r for r in rec.rows if r.field == "payout_status"][0]
    assert status.executed == "FAILED"


def test_a_poll_failure_while_paid_leaves_paid_alone_and_is_logged():
    w = World()
    ex = _paid(w)
    from rate_locked.awx.adapter import UncertainOutcome

    def boom(_tid):
        raise UncertainOutcome("get_transfer", "", "timeout")
    w.fake.get_transfer = boom
    ex = w.executor.advance(ex.execution_id)
    assert ex.state is State.PAID
    assert "poll_failed" in kinds(ex)


def test_a_status_that_goes_backwards_after_paid_is_flagged_and_not_hidden():
    w = World()
    ex = _paid(w)
    t = w.fake._transfers[ex.transfer.transfer_id]
    from dataclasses import replace
    w.fake._transfers[ex.transfer.transfer_id] = replace(t, status="PROCESSING")
    ex = w.executor.advance(ex.execution_id)
    assert ex.state is State.PAID  # nothing is invented
    assert "status_changed_after_paid" in kinds(ex)
    assert ex.transfer.status == "PROCESSING"  # shown verbatim in the record

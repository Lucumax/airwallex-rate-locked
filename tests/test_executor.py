import json
from decimal import Decimal as D

import pytest

from rate_locked.executor import EventLog, State, StateError, TRANSITIONS
from rate_locked.models import Confidence
from rate_locked.planner import make_plan
from rate_locked.scenario import demo_inputs

from helpers import World


def kinds(ex):
    return [e.kind for e in ex.events]


# ---- happy paths -------------------------------------------------------------------------------

def test_convert_then_pay_uses_the_approved_quote_and_reports_provider_status():
    w = World()
    ex = w.approved("O2")
    quote = ex.envelope.quote
    ex = w.executor.dispatch(ex.execution_id)
    assert ex.state is State.PAYOUT_REPORTED
    assert ex.conversion.quote_id == quote.quote_id and ex.conversion.client_rate == quote.client_rate
    assert ex.conversion.status == "SETTLED"
    assert ex.transfer.status == "PROCESSING"  # never presented as PAID
    assert w.executor.call_counts(ex.execution_id) == {"conversion_create": 1, "transfer_create": 1}
    assert w.balances()["USD"] == D("1700.00") and w.balances()["GBP"] == D("0.00")
    paid = w.fake.simulate_transfer_transition(ex.transfer.transfer_id, "PAID")
    assert paid.status == "PAID"
    ex = w.executor.advance(ex.execution_id)
    assert ex.state is State.PAID


def test_fund_path_makes_no_conversion_call():
    w = World()
    ex = w.approved("O1")
    assert ex.envelope.quote is None
    ex = w.executor.dispatch(ex.execution_id)
    assert ex.state is State.PAYOUT_REPORTED
    assert w.executor.call_counts(ex.execution_id) == {"conversion_create": 0, "transfer_create": 1}
    assert w.balances()["EUR"] == D("300.00")


def test_only_fund_and_convert_decisions_can_be_prepared():
    w = World()
    esc_plan = make_plan(w.inputs.with_receipt_confidence("R1", Confidence.LOW))
    with pytest.raises(Exception) as e:
        w.executor.prepare(esc_plan.decision("O3"), w.ob("O3"), esc_plan.digest)
    assert getattr(e.value, "code", "") == "NOT_EXECUTABLE"
    with pytest.raises(Exception):
        w.executor.prepare(w.plan.decision("O3"), w.ob("O3"), w.plan.digest)  # DEFER


# ---- stale approval: the wow moment ----------------------------------------------------------

def test_stale_approval_makes_zero_financial_calls_then_requote_shows_the_difference():
    w = World()
    prep = w.prepare("O2", validity="MIN_1")
    w.clock.advance(61)  # the quote lapses while nobody approves
    out = w.executor.approve(prep.token, prep.execution.envelope.digest, "approver-1")
    assert not out.ok and out.code == "EXPIRED" and "Nothing was converted" in out.message
    ex = prep.execution
    assert ex.state is State.EXPIRED
    assert w.executor.call_counts(ex.execution_id) == {"conversion_create": 0, "transfer_create": 0}
    assert not any(c.op in ("create_conversion", "create_transfer") for c in w.executor.calls.log)
    w.fake.set_rate("GBPUSD", "1.31")
    prep2, diff = w.executor.requote(ex.execution_id)
    new = prep2.execution
    assert prep2.token and new.envelope.quote.quote_id != ex.envelope.quote.quote_id
    assert diff.old_sell == D("1300.00") and diff.new_sell == D("1310.00")
    assert diff.delta == D("10.00") and diff.direction == "more"
    assert new.supersedes == ex.execution_id and "requoted" in kinds(ex)


def test_requote_with_unchanged_rate_reports_no_change():
    w = World()
    prep = w.prepare("O2")
    w.clock.advance(61)
    w.executor.approve(prep.token, prep.execution.envelope.digest, "a")
    _new, diff = w.executor.requote(prep.execution.execution_id)
    assert diff.delta == D("0.00") and diff.direction == "none"


def test_requote_is_refused_once_a_financial_call_was_made():
    w = World()
    ex = w.approved("O2")
    ex = w.executor.dispatch(ex.execution_id)
    with pytest.raises(StateError):
        w.executor.requote(ex.execution_id)


def test_approval_that_goes_stale_before_dispatch_makes_zero_calls():
    w = World()
    ex = w.approved("O2")      # approved at t+0, expires at t+50
    w.clock.advance(55)
    ex = w.executor.dispatch(ex.execution_id)
    assert ex.state is State.EXPIRED
    assert w.executor.call_counts(ex.execution_id) == {"conversion_create": 0, "transfer_create": 0}


def test_provider_side_change_of_the_quote_voids_before_any_call():
    w = World()
    ex = w.approved("O2")
    from dataclasses import replace
    q = w.fake._quotes[ex.envelope.quote.quote_id]
    w.fake._quotes[q.quote_id] = replace(q, client_rate=D("1.99"))
    ex = w.executor.dispatch(ex.execution_id)
    assert ex.state is State.VOIDED
    assert w.executor.call_counts(ex.execution_id) == {"conversion_create": 0, "transfer_create": 0}


# ---- single use ---------------------------------------------------------------------------------

def test_approval_link_works_once():
    w = World()
    prep = w.prepare("O2")
    assert w.executor.approve(prep.token, prep.execution.envelope.digest, "a").ok
    again = w.executor.approve(prep.token, prep.execution.envelope.digest, "a")
    assert not again.ok and again.code == "NONCE_USED" and again.message == "This approval link was already used."


def test_double_dispatch_is_harmless():
    w = World()
    ex = w.approved("O2")
    w.executor.dispatch(ex.execution_id)
    w.executor.dispatch(ex.execution_id)
    w.executor.dispatch(ex.execution_id)
    assert w.executor.call_counts(ex.execution_id) == {"conversion_create": 1, "transfer_create": 1}
    assert "dispatch_ignored" in kinds(ex)


def test_dispatch_requires_an_approval():
    w = World()
    prep = w.prepare("O2")
    ex = w.executor.dispatch(prep.execution.execution_id)  # never approved
    assert ex.state is State.AWAITING_APPROVAL
    assert w.executor.call_counts(ex.execution_id) == {"conversion_create": 0, "transfer_create": 0}


def test_denied_approval_stops_everything():
    w = World()
    prep = w.prepare("O2")
    ex = w.executor.deny(prep.token, "approver-1")
    assert ex.state is State.DENIED
    ex = w.executor.dispatch(ex.execution_id)
    assert ex.state is State.DENIED
    assert w.executor.call_counts(ex.execution_id) == {"conversion_create": 0, "transfer_create": 0}


def test_changed_terms_at_approval_void_the_request():
    w = World()
    prep = w.prepare("O2")
    out = w.executor.approve(prep.token, "f" * 64, "a")
    assert not out.ok and out.code == "TERMS_CHANGED" and out.execution.state is State.VOIDED


# ---- uncertain outcomes: look up, never retry -------------------------------------------------------

def test_lost_conversion_response_is_looked_up_not_resent():
    w = World()
    ex = w.approved("O2")
    w.fake.inject("create_conversion", "drop_response")
    ex = w.executor.dispatch(ex.execution_id)
    assert ex.state is State.UNKNOWN and ex.unknown_stage == "conversion"
    w.executor.dispatch(ex.execution_id)  # a second click does not create again
    assert w.executor.call_counts(ex.execution_id)["conversion_create"] == 1
    ex = w.executor.reconcile(ex.execution_id)
    assert ex.state is State.PAYOUT_REPORTED
    assert w.executor.call_counts(ex.execution_id) == {"conversion_create": 1, "transfer_create": 1}
    assert w.balances()["USD"] == D("1700.00")  # one conversion's worth, not two


def test_request_lost_before_effect_stays_unresolved_and_never_resends():
    w = World()
    ex = w.approved("O2")
    w.fake.inject("create_conversion", "drop_request")
    ex = w.executor.dispatch(ex.execution_id)
    assert ex.state is State.UNKNOWN
    for _ in range(2):
        ex = w.executor.reconcile(ex.execution_id)
        assert ex.state is State.UNKNOWN
    ex = w.executor.reconcile(ex.execution_id)
    assert ex.state is State.INDETERMINATE
    ex = w.executor.dispatch(ex.execution_id)
    assert ex.state is State.INDETERMINATE
    assert w.executor.call_counts(ex.execution_id) == {"conversion_create": 1, "transfer_create": 0}
    assert w.balances()["USD"] == D("3000.00")


def test_empty_lookup_is_not_final_when_the_provider_lags():
    w = World(lookup_lag=3)
    ex = w.approved("O2")
    w.fake.inject("create_conversion", "drop_response")
    ex = w.executor.dispatch(ex.execution_id)
    for _ in range(3):
        ex = w.executor.reconcile(ex.execution_id)
    assert ex.state is State.INDETERMINATE  # three empty answers, still no resend
    ex = w.executor.reconcile(ex.execution_id)  # the record becomes visible
    assert ex.state is State.PAYOUT_REPORTED
    assert w.executor.call_counts(ex.execution_id) == {"conversion_create": 1, "transfer_create": 1}


def test_lost_payout_response_is_looked_up_not_resent():
    w = World()
    ex = w.approved("O2")
    w.fake.inject("create_transfer", "drop_response")
    ex = w.executor.dispatch(ex.execution_id)
    assert ex.state is State.UNKNOWN and ex.unknown_stage == "payout"
    ex = w.executor.reconcile(ex.execution_id)
    assert ex.state is State.PAYOUT_REPORTED and ex.transfer.status == "PROCESSING"
    assert w.executor.call_counts(ex.execution_id) == {"conversion_create": 1, "transfer_create": 1}
    assert w.balances()["GBP"] == D("0.00")  # paid out exactly once


def test_lost_in_memory_progress_does_not_create_again():
    w = World()
    ex = w.approved("O2")
    # simulate a crash: state says CONVERTING but no conversion object was stored
    w.executor._force_state(ex, State.CONVERTING)
    ex = w.executor.advance(ex.execution_id)
    assert ex.state is State.UNKNOWN and ex.unknown_stage == "conversion"
    assert w.executor.call_counts(ex.execution_id)["conversion_create"] == 0


# ---- failures are visible and not retried -------------------------------------------------------

def test_payout_refused_after_conversion_is_a_visible_failure_with_no_retry():
    w = World()
    ex = w.approved("O2")
    # make the provider refuse the payout by removing the beneficiary
    w.fake._beneficiaries.clear()
    ex = w.executor.dispatch(ex.execution_id)
    assert ex.state is State.PAYOUT_FAILED
    assert "needs_person_after_conversion" in kinds(ex)
    assert w.executor.call_counts(ex.execution_id) == {"conversion_create": 1, "transfer_create": 1}
    w.executor.advance(ex.execution_id)
    assert w.executor.call_counts(ex.execution_id)["transfer_create"] == 1


def test_provider_reported_payout_failure_is_shown_as_failed():
    w = World()
    ex = w.approved("O1")
    ex = w.executor.dispatch(ex.execution_id)
    w.fake.simulate_transfer_transition(ex.transfer.transfer_id, "FAILED")
    ex = w.executor.advance(ex.execution_id)
    assert ex.state is State.PAYOUT_FAILED and ex.transfer.status == "FAILED"


def test_conversion_still_scheduled_waits_and_pays_only_after_settled():
    w = World(settle_after_polls=2)
    ex = w.approved("O2")
    ex = w.executor.dispatch(ex.execution_id)
    assert ex.state is State.CONVERTING and w.executor.call_counts(ex.execution_id)["transfer_create"] == 0
    ex = w.executor.advance(ex.execution_id)
    assert w.executor.call_counts(ex.execution_id)["transfer_create"] in (0, 1)
    for _ in range(3):
        ex = w.executor.advance(ex.execution_id)
    assert ex.state is State.PAYOUT_REPORTED
    assert w.executor.call_counts(ex.execution_id) == {"conversion_create": 1, "transfer_create": 1}


def test_payout_waits_if_funds_are_not_confirmed_available():
    w = World()
    ex = w.approved("O2")
    w.fake._balances["GBP"] = D("0")
    # settle, then take the funds away before the payout step
    orig = w.fake.get_balances
    w.fake.get_balances = lambda: [b for b in orig() if b.currency != "GBP"]
    ex = w.executor.dispatch(ex.execution_id)
    assert ex.state is State.FUNDS_READY
    assert w.executor.call_counts(ex.execution_id)["transfer_create"] == 0
    assert "payout_waiting_for_funds" in kinds(ex)


# ---- state machine hygiene ----------------------------------------------------------------------------

def test_illegal_transitions_raise():
    w = World()
    prep = w.prepare("O2")
    with pytest.raises(StateError):
        w.executor._transition(prep.execution, State.PAID, "test")
    with pytest.raises(StateError):
        w.executor._transition(prep.execution, State.CONVERTING, "skipped approval")


def test_terminal_states_have_no_exits_except_reconcile_and_payout_updates():
    for terminal in (State.DENIED, State.EXPIRED, State.VOIDED, State.CONVERSION_FAILED, State.PAYOUT_FAILED):
        assert TRANSITIONS[terminal] == frozenset()
    assert TRANSITIONS[State.PAID] == frozenset({State.PAYOUT_FAILED})  # PAID is a report, not final
    assert State.INDETERMINATE in TRANSITIONS and TRANSITIONS[State.INDETERMINATE]


def test_write_ahead_events_come_before_the_calls_and_log_is_jsonl(tmp_path):
    path = tmp_path / "events.jsonl"
    w = World()
    w.executor.log = EventLog(path)
    ex = w.approved("O2")
    w.executor.dispatch(ex.execution_id)
    order = kinds(ex)
    assert order.index("conversion_dispatching") < order.index("conversion_created")
    assert order.index("transfer_dispatching") < order.index("transfer_created")
    lines = [json.loads(x) for x in path.read_text().splitlines()]
    assert lines and all({"seq", "at", "execution_id", "kind"} <= set(x) for x in lines)
    assert "token" not in path.read_text().lower()


def test_events_never_contain_the_approval_token():
    w = World()
    prep = w.prepare("O2")
    w.executor.approve(prep.token, prep.execution.envelope.digest, "a")
    w.executor.dispatch(prep.execution.execution_id)
    blob = json.dumps([e.data for e in prep.execution.events], default=str)
    assert prep.token not in blob


# ---- provider terms are compared before the payout (package review R11) ------------------------------

def test_a_settled_conversion_whose_terms_differ_from_the_approved_quote_stops_before_the_payout():
    from dataclasses import replace as _replace
    w = World()
    ex = w.approved("O2")
    orig = w.fake.get_conversion
    w.fake.get_conversion = lambda cid: _replace(orig(cid), client_rate=orig(cid).client_rate + D("0.01"))
    ex = w.executor.dispatch(ex.execution_id)
    assert w.executor.call_counts(ex.execution_id) == {"conversion_create": 1, "transfer_create": 0}
    assert ex.state is State.INDETERMINATE
    differ = [e for e in ex.events if e.kind == "conversion_terms_differ"]
    assert differ and differ[0].data["fields"] == ["client_rate"]
    assert "needs_person_after_conversion" in kinds(ex)
    ex = w.executor.advance(ex.execution_id)          # advancing again never sends the payout
    assert w.executor.call_counts(ex.execution_id)["transfer_create"] == 0


def test_a_settled_conversion_with_the_approved_terms_still_pays():
    w = World()
    ex = w.approved("O2")
    ex = w.executor.dispatch(ex.execution_id)
    assert "conversion_terms_differ" not in kinds(ex) and ex.state is State.PAYOUT_REPORTED


# ---- provider transfer terms are compared before PAID is shown ------------

def test_a_transfer_reply_with_other_terms_stops_for_a_person_instead_of_paid():
    from dataclasses import replace as _replace
    w = World()
    ex = w.approved("O2")
    orig = w.fake.create_transfer
    w.fake.create_transfer = lambda **kw: _replace(orig(**kw), status="PAID",
                                                   transfer_amount=D("999.00"))
    ex = w.executor.dispatch(ex.execution_id)
    assert ex.state is State.INDETERMINATE and ex.state is not State.PAID
    differ = [e for e in ex.events if e.kind == "transfer_terms_differ"]
    assert differ and differ[0].data["fields"] == ["transfer_amount"]
    assert w.executor.call_counts(ex.execution_id)["transfer_create"] == 1
    ex = w.executor.reconcile(ex.execution_id)            # the lookup finds the fake's own record (not PAID): nothing is resent
    assert ex.state is not State.PAID and w.executor.call_counts(ex.execution_id)["transfer_create"] == 1


def test_a_polled_transfer_with_other_terms_is_not_promoted_to_paid():
    from dataclasses import replace as _replace
    w = World()
    ex = w.approved("O2")
    ex = w.executor.dispatch(ex.execution_id)
    assert ex.state is State.PAYOUT_REPORTED
    orig = w.fake.get_transfer
    w.fake.get_transfer = lambda tid: _replace(orig(tid), status="PAID", transfer_currency="EUR")
    ex = w.executor.advance(ex.execution_id)
    assert ex.state is State.INDETERMINATE
    assert [e.data["fields"] for e in ex.events if e.kind == "transfer_terms_differ"] == [["transfer_currency"]]


def test_a_transfer_with_the_approved_terms_still_reaches_paid():
    w = World()
    ex = w.executor.dispatch(w.approved("O2").execution_id)
    w.fake.simulate_transfer_transition(ex.transfer.transfer_id, "PAID")
    ex = w.executor.advance(ex.execution_id)
    assert ex.state is State.PAID and "transfer_terms_differ" not in kinds(ex)


def test_a_late_failure_after_paid_is_still_shown_when_the_reply_also_changes_a_term():
    from dataclasses import replace as _replace
    w = World()
    ex = w.executor.dispatch(w.approved("O2").execution_id)
    w.fake.simulate_transfer_transition(ex.transfer.transfer_id, "PAID")
    ex = w.executor.advance(ex.execution_id)
    assert ex.state is State.PAID
    orig = w.fake.get_transfer
    w.fake.get_transfer = lambda tid: _replace(orig(tid), status="FAILED", fee_amount=D("1.00"))
    ex = w.executor.advance(ex.execution_id)
    assert ex.state is State.PAYOUT_FAILED                       # the confirmed failure is not hidden
    assert "transfer_terms_differ" in kinds(ex) and "late_failure_after_paid" in kinds(ex)
    assert w.executor.call_counts(ex.execution_id)["transfer_create"] == 1


def test_an_expired_fund_request_does_not_speak_of_a_rate():
    w = World()
    ex = w.prepare("O1").execution
    assert ex.envelope.quote is None
    w.clock.advance(24 * 3600)
    w.executor.lapse_overdue()
    assert ex.state is State.EXPIRED
    assert "rate" not in ex.text.lower() and "nothing was paid" in ex.text.lower()


# ---- a same-request FAILED that also changes a term is never cleared to PAID --------

def _failed_with_other_fee(t):
    from dataclasses import replace as _replace
    return _replace(t, status="FAILED", fee_amount=D("1.00"))


def _later_matching_paid_is_not_adopted(w, ex):
    assert ex.state is State.PAYOUT_FAILED                        # the observed failure is kept
    assert "transfer_terms_differ" in kinds(ex) and "needs_person_after_payout" in kinds(ex)
    w.fake.simulate_transfer_transition(ex.transfer.transfer_id, "PAID")
    for step in (w.executor.reconcile, w.executor.advance, w.executor.dispatch):
        ex = step(ex.execution_id)
        assert ex.state is State.PAYOUT_FAILED                    # a later matching PAID never clears it
    assert w.executor.call_counts(ex.execution_id)["transfer_create"] == 1


def test_a_failed_create_reply_with_other_terms_is_not_cleared_by_a_later_paid_lookup():
    w = World()
    ex = w.approved("O2")
    orig = w.fake.create_transfer
    w.fake.create_transfer = lambda **kw: _failed_with_other_fee(orig(**kw))
    ex = w.executor.dispatch(ex.execution_id)
    w.fake.create_transfer = orig
    _later_matching_paid_is_not_adopted(w, ex)


def test_a_failed_poll_with_other_terms_is_not_cleared_by_a_later_paid_lookup():
    w = World()
    ex = w.executor.dispatch(w.approved("O2").execution_id)
    assert ex.state is State.PAYOUT_REPORTED
    orig = w.fake.get_transfer
    w.fake.get_transfer = lambda tid: _failed_with_other_fee(orig(tid))
    ex = w.executor.advance(ex.execution_id)
    w.fake.get_transfer = orig
    _later_matching_paid_is_not_adopted(w, ex)


def test_a_failed_lookup_with_other_terms_is_not_cleared_by_a_later_paid_lookup():
    w = World()
    ex = w.approved("O2")
    w.fake.inject("create_transfer", "drop_response")
    ex = w.executor.dispatch(ex.execution_id)
    assert ex.state is State.UNKNOWN and ex.unknown_stage == "payout"
    orig = w.fake.find_transfers
    w.fake.find_transfers = lambda rid, **kw: [_failed_with_other_fee(t) for t in orig(rid, **kw)]
    ex = w.executor.reconcile(ex.execution_id)
    w.fake.find_transfers = orig
    _later_matching_paid_is_not_adopted(w, ex)


def test_a_lookup_that_reports_paid_with_other_terms_is_not_promoted_to_paid():
    from dataclasses import replace as _replace
    w = World()
    ex = w.approved("O2")
    w.fake.inject("create_transfer", "drop_response")
    ex = w.executor.dispatch(ex.execution_id)
    w.fake.simulate_transfer_transition(w.fake.find_transfers(ex.envelope.transfer_request_id)[0].transfer_id, "PAID")
    orig = w.fake.find_transfers
    w.fake.find_transfers = lambda rid, **kw: [_replace(t, transfer_amount=D("999.00")) for t in orig(rid, **kw)]
    ex = w.executor.reconcile(ex.execution_id)
    assert ex.state is State.INDETERMINATE and ex.transfer.status == "PAID"
    assert [e.data["fields"] for e in ex.events if e.kind == "transfer_terms_differ"] == [["transfer_amount"]]


def test_repeated_polls_of_the_same_difference_raise_one_review_not_one_per_poll():
    from dataclasses import replace as _replace
    w = World()
    ex = w.executor.dispatch(w.approved("O2").execution_id)
    w.fake.simulate_transfer_transition(ex.transfer.transfer_id, "PAID")
    ex = w.executor.advance(ex.execution_id)
    assert ex.state is State.PAID
    orig = w.fake.get_transfer
    w.fake.get_transfer = lambda tid: _replace(orig(tid), fee_amount=D("1.00"))
    for _ in range(3):
        ex = w.executor.advance(ex.execution_id)
    assert kinds(ex).count("transfer_terms_differ") == 3          # each observation is kept
    assert kinds(ex).count("needs_person_after_paid") == 1        # one review, not three
    w.fake.get_transfer = lambda tid: _replace(orig(tid), status="FAILED", fee_amount=D("1.00"))
    for _ in range(3):
        ex = w.executor.advance(ex.execution_id)
    assert ex.state is State.PAYOUT_FAILED and kinds(ex).count("late_failure_after_paid") == 1


def test_a_failed_lookup_naming_another_beneficiary_is_not_cleared_by_a_later_paid_lookup():
    from dataclasses import replace as _replace
    w = World()
    ex = w.approved("O2")
    w.fake.inject("create_transfer", "drop_response")
    ex = w.executor.dispatch(ex.execution_id)
    assert ex.state is State.UNKNOWN and ex.unknown_stage == "payout"
    orig = w.fake.find_transfers
    w.fake.find_transfers = lambda rid, **kw: [_replace(t, status="FAILED", beneficiary_id="ben_other")
                                               for t in orig(rid, **kw)]
    ex = w.executor.reconcile(ex.execution_id)
    w.fake.find_transfers = orig
    assert [e.data["fields"] for e in ex.events if e.kind == "transfer_terms_differ"] == [["beneficiary_id"]]
    _later_matching_paid_is_not_adopted(w, ex)


def test_a_failure_seen_among_duplicate_lookup_records_holds_a_later_paid_for_a_person():
    from dataclasses import replace as _replace
    w = World()
    ex = w.approved("O2")
    w.fake.inject("create_transfer", "drop_response")
    ex = w.executor.dispatch(ex.execution_id)
    orig = w.fake.find_transfers
    w.fake.find_transfers = lambda rid, **kw: [r for t in orig(rid, **kw) for r in
                                               (_replace(t, status="FAILED", beneficiary_id="ben_other"), t)]
    ex = w.executor.reconcile(ex.execution_id)
    w.fake.find_transfers = orig
    assert ex.state is State.INDETERMINATE
    w.fake.simulate_transfer_transition(w.fake.find_transfers(ex.envelope.transfer_request_id)[0].transfer_id, "PAID")
    for _ in range(2):
        ex = w.executor.reconcile(ex.execution_id)
        assert ex.state is State.INDETERMINATE                    # the failure seen earlier is not cleared by a PAID
    assert kinds(ex).count("needs_person_after_payout") == 1
    assert w.executor.call_counts(ex.execution_id)["transfer_create"] == 1


# ---- a poll by transfer id never replaces the stored transfer with another id ----------

def test_a_poll_after_paid_that_returns_a_foreign_transfer_keeps_watching_the_original():
    from dataclasses import replace as _replace
    w = World()
    ex = w.executor.dispatch(w.approved("O2").execution_id)
    w.fake.simulate_transfer_transition(ex.transfer.transfer_id, "PAID")
    ex = w.executor.advance(ex.execution_id)
    a = ex.transfer.transfer_id
    orig = w.fake.get_transfer
    w.fake.get_transfer = lambda tid: _replace(orig(tid), transfer_id="tr_other", request_id="req_other",
                                               status="FAILED")
    ex = w.executor.advance(ex.execution_id)
    w.fake.get_transfer = orig
    assert ex.state is State.PAID and ex.transfer.transfer_id == a      # the foreign record is not adopted
    assert "poll_returned_other_transfer" in kinds(ex) and "needs_person_after_paid" in kinds(ex)
    w.fake.simulate_transfer_transition(a, "FAILED")
    ex = w.executor.advance(ex.execution_id)
    assert ex.state is State.PAYOUT_FAILED                              # the original's late failure is still read
    assert w.executor.call_counts(ex.execution_id)["transfer_create"] == 1


def test_a_poll_that_returns_another_transfer_id_for_the_same_request_goes_to_a_person_not_failed():
    from dataclasses import replace as _replace
    w = World()
    ex = w.executor.dispatch(w.approved("O2").execution_id)
    assert ex.state is State.PAYOUT_REPORTED
    a = ex.transfer.transfer_id
    orig = w.fake.get_transfer
    w.fake.get_transfer = lambda tid: _replace(orig(tid), transfer_id="tr_other", status="FAILED")
    ex = w.executor.advance(ex.execution_id)
    w.fake.get_transfer = orig
    assert ex.state is State.INDETERMINATE and ex.transfer.transfer_id == a  # not PAYOUT_FAILED: no re-approval
    assert "poll_returned_other_transfer" in kinds(ex) and "needs_person_after_payout" in kinds(ex)
    with pytest.raises(StateError):
        w.prepare("O2")                                                 # the obligation stays blocked
    w.fake.simulate_transfer_transition(a, "PAID")
    ex = w.executor.reconcile(ex.execution_id)                          # the lookup by request id finds A
    assert ex.state is State.PAID
    assert w.executor.call_counts(ex.execution_id)["transfer_create"] == 1

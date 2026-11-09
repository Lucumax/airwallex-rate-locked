"""Console session: one adapter, gate, executor, reader and plan history behind the web console.

Runs on the local fake with a manual clock. The session reuses the core money path unchanged; these tests pin
what the six screens show: invoices with source words, the three-obligation plan with recalculation, the quote
clock approver, the refused stale approval with zero calls, approved versus executed, and lookup plus Verify.
"""
import json

import pytest

from rate_locked.awx.fake import FakeAirwallex
from rate_locked.clock import ManualClock
from rate_locked.scenario import NOW, demo_inputs
from rate_locked.session import DIGEST_PREFIX, Session, SessionError

from helpers import REVIEW, resolve_review


def make(controls=True, resolve=True):
    """A console session. By default the person has already resolved the one review flag (see the review tests)."""
    clock = ManualClock(NOW)
    fake = FakeAirwallex.demo(clock=clock)
    s = Session(fake, world="FAKE", clock=clock, fake_controls=fake if controls else None,
                run_id="r-test", commit="abc1234")
    if resolve:
        resolve_review(s)
    return s


# ---- the review flag: one invoice waits for a person, and so does the plan --------------------------------

def test_one_invoice_waits_for_review_and_the_plan_waits_with_it():
    s = make(resolve=False)
    st = s.state()
    json.dumps(st)
    assert [i["ready"] for i in st["invoices"]] == [True, True, False]
    inv = st["invoices"][2]
    assert inv["invoice_id"] == "inv-initech" and inv["review"]["status"] == "PENDING"
    due = next(f for f in inv["fields"] if f["name"] == "due_date")
    assert due["status"] == "REVIEW" and due["flags"] == ["TIMEZONE_NOT_STATED"]
    assert due["local"] == "2026-11-04T21:00"  # the wall time in the source words, without the assumed zone
    assert inv["text"][due["start"]:due["end"]] == due["quote"]
    assert st["review"]["pending"] == ["inv-initech"] and st["review"]["resolved"] == []
    assert st["plans"] == [] and st["explanation"] is None and st["reserve"] is None
    assert [o["obligation_id"] for o in st["obligations"]] == ["O1", "O2"]
    assert st["sources"]["review"] == "HUMAN"
    for call in (lambda: s.prepare("O1"), lambda: s.lower_confidence("R1"),
                 lambda: s.simulate_deposit("USD", "1000")):
        with pytest.raises(SessionError) as e:
            call()
        assert e.value.status == 409 and e.value.code == "REVIEW_PENDING"
    ops = {c["op"] for c in s.state()["calls"]}
    assert ops <= {"create_beneficiary"}, ops  # nothing quoted, converted or paid while the review is open


def test_person_resolves_the_flag_then_plan_v1_is_built():
    s = make(resolve=False)
    out = resolve_review(s)
    assert out["invoice_id"] == "inv-initech" and out["ready"] is True and out["plan_version"] == 1
    st = s.state()
    assert [i["ready"] for i in st["invoices"]] == [True, True, True]
    inv = st["invoices"][2]
    assert inv["review"]["status"] == "RESOLVED"
    due = next(f for f in inv["fields"] if f["name"] == "due_date")
    assert due["status"] == "RESOLVED" and due["value"] == "2026-11-04T21:00:00+00:00"
    assert due["resolution"]["note"] == REVIEW["note"] and due["resolution"]["by"] == "console operator"
    assert due["resolution"]["flags_before"] == ["TIMEZONE_NOT_STATED"] and due["resolution"]["at"]
    res = st["review"]["resolved"]
    assert len(res) == 1 and res[0]["invoice_id"] == "inv-initech" and res[0]["field"] == "due_date"
    assert st["review"]["pending"] == []
    assert acts(st["plans"][-1]) == {"O1": "FUND", "O2": "CONVERT", "O3": "DEFER"}
    assert len(st["plans"]) == 1 and any("confirmed by a person" in t for t in st["plans"][0]["trigger"])
    seed = {o.obligation_id: o for o in demo_inputs().obligations}
    o3 = next(o for o in st["obligations"] if o["obligation_id"] == "O3")
    assert o3["due_at"] == seed["O3"].due_at.isoformat() and o3["supplier_name"] == "Initech Pte. Ltd."
    assert [o["obligation_id"] for o in st["obligations"]] == ["O1", "O2", "O3"]
    assert s.prepare("O2")["token"]


def test_resolution_cannot_change_the_source_words():
    s = make(resolve=False)
    cases = [
        ({"value": "2026-11-05T21:00:00+00:00"}, 400, "VALUE_NOT_IN_QUOTE"),   # another day
        ({"value": "2026-11-04T20:00:00+00:00"}, 400, "VALUE_NOT_IN_QUOTE"),   # another hour
        ({"value": "2026-11-04T21:00:00"}, 400, "UNREADABLE_VALUE"),           # still no zone
        ({"value": "soon"}, 400, "UNREADABLE_VALUE"),
        ({"note": "  "}, 400, "NOTE_REQUIRED"),
        ({"note": "x" * 201}, 400, "NOTE_TOO_LONG"),
        ({"field": "amount", "value": "2000.00"}, 409, "FIELD_NOT_FLAGGED"),
        ({"field": "colour"}, 400, "UNKNOWN_FIELD"),
        ({"invoice_id": "inv-nope"}, 404, "UNKNOWN_INVOICE"),
        ({"invoice_id": "inv-acme"}, 409, "NOT_IN_REVIEW"),
    ]
    for over, status, code in cases:
        with pytest.raises(SessionError) as e:
            resolve_review(s, **over)
        assert (e.value.status, e.value.code) == (status, code), over
    assert s.state()["review"]["pending"] == ["inv-initech"] and s.state()["plans"] == []
    resolve_review(s)
    with pytest.raises(SessionError) as e:
        resolve_review(s)
    assert (e.value.status, e.value.code) == (409, "NOT_IN_REVIEW")
    assert len(s.state()["plans"]) == 1


def test_a_different_zone_gives_a_different_deadline_in_utc():
    s = make(resolve=False)
    resolve_review(s, value="2026-11-04T21:00:00+08:00", note="Supplier terms: times are UTC+08:00")
    o3 = next(o for o in s.state()["obligations"] if o["obligation_id"] == "O3")
    assert o3["due_at"] == "2026-11-04T13:00:00+00:00"
    assert len(s.state()["plans"]) == 1


def acts(plan):
    return {d["obligation_id"]: d["action"] for d in plan["decisions"]}


def approve(s, token, digest=None, typed=None):
    view = s.approver_view(token)
    digest = digest or view["digest"]
    return s.approver_approve(token, digest, typed if typed is not None else digest[:DIGEST_PREFIX])


def test_state_is_json_and_labels_every_source():
    st = make().state()
    json.dumps(st)
    assert st["chips"]["world"] == "FAKE"
    assert "TEST DOUBLE" in st["chips"]["reader"]
    assert st["chips"]["run"].startswith("LIVE r-test")
    assert st["sources"]["balances"] == "FAKE" and st["sources"]["receipts"] == "FIXTURE"
    assert st["sources"]["reader"] == "TEST DOUBLE" and st["sources"]["plan"] == "CODE"


def test_obligations_come_from_the_validated_reader_output():
    st = make().state()
    assert [i["ready"] for i in st["invoices"]] == [True, True, True]
    seed = {o.obligation_id: o for o in demo_inputs().obligations}
    for ob in st["obligations"]:
        s = seed[ob["obligation_id"]]
        assert (ob["supplier_name"], ob["amount"], ob["currency"], ob["due_at"], ob["invoice_ref"]) == (
            s.supplier_name, f"{s.amount:.2f}", s.currency, s.due_at.isoformat(), s.invoice_ref)
    inv = st["invoices"][0]
    for f in inv["fields"]:
        assert inv["text"][f["start"]:f["end"]] == f["quote"]


def test_plan_recalculates_only_affected_obligations():
    s = make()
    assert acts(s.state()["plans"][-1]) == {"O1": "FUND", "O2": "CONVERT", "O3": "DEFER"}
    s.lower_confidence("R1")
    p2 = s.state()["plans"][-1]
    assert p2["version"] == 2 and acts(p2)["O3"] == "ESCALATE"
    assert p2["reused"] == ["O1"] and "O3" in p2["changed"]
    assert any(d["field"] == "R1 confidence" and d["before"] == "HIGH" and d["after"] == "LOW" for d in p2["diff"])
    s.simulate_deposit("USD", "1000")
    p3 = s.state()["plans"][-1]
    assert p3["version"] == 3 and acts(p3)["O3"] == "CONVERT" and p3["reused"] == ["O1"]
    assert any(d["field"] == "USD balance" and d["before"] == "3,000.00" and d["after"] == "4,000.00"
               for d in p3["diff"])
    assert len({p["digest"] for p in s.state()["plans"]}) == 3
    assert any(c["op"] == "simulate_deposit" for c in s.state()["calls"])


def test_triggers_cannot_repeat():
    s = make()
    s.lower_confidence("R1")
    with pytest.raises(SessionError) as e:
        s.lower_confidence("R1")
    assert e.value.status == 409


def test_explanation_is_checked_against_plan_facts():
    ex = make().state()["explanation"]
    assert ex["problems"] == [] and ex["accepted"] is True and "TEST DOUBLE" in ex["reader"]


def test_stale_approval_is_refused_with_zero_calls_then_requoted():
    s = make()
    prep = s.prepare("O2")
    tok = prep["token"]
    view = s.approver_view(tok)
    assert view["state"] == "live" and view["mode"] == "CONVERT" and view["quote_id"]
    assert view["you_pay"]["currency"] == "USD" and view["supplier_gets"]["currency"] == "GBP"
    s.skip_clock(61)
    assert s.approver_view(tok)["state"] == "expired"
    out = approve(s, tok)
    assert out["ok"] is False and out["code"] == "EXPIRED"
    ex = s.execution(prep["execution_id"])
    assert ex["state"] == "EXPIRED" and ex["counts"] == {"conversion_create": 0, "transfer_create": 0}
    s.set_fake_rate("GBPUSD", "1.31")
    new = s.requote(prep["execution_id"])
    assert new["difference"]["text"] == "Difference: USD 10.00 more"
    assert s.execution(prep["execution_id"])["requote"]["new_execution"] == new["execution_id"]
    assert s.approver_view(new["token"])["state"] == "live"


def test_inside_dispatch_margin_shows_too_close():
    s = make()
    prep = s.prepare("O2")
    s.skip_clock(55)
    assert s.approver_view(prep["token"])["state"] == "too_close"


def test_wrong_typed_prefix_does_not_use_the_link():
    s = make()
    prep = s.prepare("O2")
    with pytest.raises(SessionError) as e:
        approve(s, prep["token"], typed="00000000")
    assert e.value.status == 400
    assert s.approver_view(prep["token"])["state"] == "live"
    assert approve(s, prep["token"])["ok"] is True


def test_changed_terms_void_the_link():
    s = make()
    prep = s.prepare("O2")
    out = approve(s, prep["token"], digest="f" * 64, typed="f" * DIGEST_PREFIX)
    assert out["code"] == "TERMS_CHANGED"
    assert s.approver_view(prep["token"])["state"] == "terms_changed"


def test_denied_link():
    s = make()
    prep = s.prepare("O2")
    s.approver_deny(prep["token"])
    assert s.approver_view(prep["token"])["state"] == "denied"
    assert s.execution(prep["execution_id"])["state"] == "DENIED"


def test_approved_versus_executed_and_old_link_used():
    s = make()
    prep = s.prepare("O2")
    assert approve(s, prep["token"])["ok"] is True
    ex = s.dispatch(prep["execution_id"])
    assert ex["state"] == "PAYOUT_REPORTED" and ex["transfer"]["status"] == "PROCESSING"
    rec = s.record(prep["execution_id"])
    status = next(r for r in rec["rows"] if r["field"] == "payout_status")
    assert status["executed"] == "PROCESSING" and status["matches"] is None
    s.simulate_transition(prep["execution_id"], "PAID")
    rec = s.record(prep["execution_id"])
    assert rec["all_match"] is True and rec["state"] == "PAID"
    assert s.approver_view(prep["token"])["state"] == "used"
    assert s.execution(prep["execution_id"])["counts"] == {"conversion_create": 1, "transfer_create": 1}
    reserve = {r["currency"]: r for r in s.state()["reserve"]["rows"]}
    assert reserve["GBP"]["available"] == "0.00"  # converted GBP 1,000.00 and paid it out


def test_reserve_before_execution_subtracts_what_the_plan_relies_on():
    rows = {r["currency"]: r for r in make().state()["reserve"]["rows"]}
    assert rows["EUR"]["available"] == "1,500.00" and rows["EUR"]["reserved"] == "1,500.00"
    assert rows["EUR"]["remaining"] == "0.00"
    assert rows["USD"]["remaining"] == "1,693.50"  # USD 3,000.00 minus the O2 estimate of USD 1,306.50


def test_withheld_response_then_lookup_by_request_id():
    s = make()
    prep = s.prepare("O1")
    approve(s, prep["token"])
    s.withhold_next_response("create_transfer")
    ex = s.dispatch(prep["execution_id"])
    assert ex["state"] == "UNKNOWN" and ex["counts"]["transfer_create"] == 1
    ex = s.reconcile(prep["execution_id"])
    assert ex["state"] == "PAYOUT_REPORTED" and ex["counts"]["transfer_create"] == 1
    kinds = [e["kind"] for e in ex["events"]]
    assert "outcome_unknown" in kinds and "lookup_found" in kinds
    assert any(c["op"] == "find_transfers" for c in s.state()["calls"])


def test_verify_pass_and_edited_copy_names_the_row():
    s = make()
    prep = s.prepare("O2")
    approve(s, prep["token"])
    s.dispatch(prep["execution_id"])
    s.simulate_transition(prep["execution_id"], "PAID")
    rec = s.record(prep["execution_id"])
    assert s.verify(rec) == {"verdict": "PASS", "problems": []}
    bad = json.loads(json.dumps(rec))
    next(r for r in bad["rows"] if r["field"] == "rate")["executed"] = "1.20"
    out = s.verify(bad)
    assert out["verdict"] == "FAIL" and any(p.startswith("RATE_MISMATCH at row rate") for p in out["problems"])
    assert s.verify({"not": "a record"})["verdict"] == "FAIL"


def test_fake_controls_absent_outside_the_fake_world():
    s = make(controls=False)
    assert s.state()["controls"] is False
    for call in (lambda: s.skip_clock(61), lambda: s.set_fake_rate("GBPUSD", "1.31"),
                 lambda: s.withhold_next_response("create_transfer")):
        with pytest.raises(SessionError) as e:
            call()
        assert e.value.status == 403


def test_controls_are_logged_in_the_manifest():
    s = make()
    s.skip_clock(61)
    s.set_fake_rate("GBPUSD", "1.31")
    log = s.state()["controls_log"]
    assert [c["control"] for c in log] == ["skip_clock", "set_fake_rate"]


def test_approver_view_carries_no_console_data():
    s = make()
    a = s.prepare("O2")
    s.prepare("O1")
    view = json.dumps(s.approver_view(a["token"]))
    assert "token" not in view and "approver_path" not in view and "plans" not in view


def test_requote_only_after_a_lapse():
    s = make()
    prep = s.prepare("O2")
    with pytest.raises(SessionError) as e:
        s.requote(prep["execution_id"])
    assert e.value.status == 409


def test_unknown_ids_are_404():
    s = make()
    with pytest.raises(SessionError) as e:
        s.execution("nope")
    assert e.value.status == 404
    with pytest.raises(SessionError) as e:
        s.approver_view("nope")
    assert e.value.status == 404
    with pytest.raises(SessionError) as e:
        s.prepare("O9")
    assert e.value.status == 404


# ---- plain-language sentence of what an approval covers (built from the bound envelope by code) -----------------

def test_in_words_states_exactly_what_the_approval_covers_and_matches_the_envelope():
    s = make()
    conv = s.prepare("O2")
    ex = s.execution(conv["execution_id"])
    q = ex["quote"]
    assert ex["in_words"] == (f"Convert USD {q['sell_amount']} into GBP {q['buy_amount']} at the locked quote "
                              f"{q['quote_id']} (rate {q['client_rate']}), then pay Globex Ltd. GBP 1,000.00 for "
                              "invoice INV-2040.")
    view = s.approver_view(conv["token"])
    assert view["in_words"] == ex["in_words"]  # the phone page says the same words as the console
    assert ex["approval_margin_s"] == view["dispatch_margin_s"]
    fund = s.prepare("O1")
    assert s.execution(fund["execution_id"])["in_words"] == "Pay Acme Co. EUR 1,200.00 for invoice INV-1001 from the EUR balance. No conversion."


# ---- a failed setup after a review answer must not leave the session stuck --------------------------------

def test_failed_beneficiary_create_after_review_can_be_retried():
    from rate_locked.awx.adapter import AirwallexError
    s = make(resolve=False)
    real = s.calls.create_beneficiary
    calls = {"n": 0}

    def flaky(**kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise AirwallexError("server_error", "temporary")
        return real(**kw)

    s.calls.create_beneficiary = flaky
    with pytest.raises(SessionError) as e:
        resolve_review(s)
    assert e.value.status == 502
    st = s.state()
    assert st["invoices"][2]["review"]["status"] == "PENDING"  # still waiting, not marked resolved
    resolve_review(s)                                          # the same answer works on retry
    st = s.state()
    assert st["invoices"][2]["review"]["status"] == "RESOLVED"
    assert [o.obligation_id for o in s.obligations] == ["O1", "O2", "O3"]
    assert s.plan.version == 1


def test_replan_after_a_payment_does_not_reserve_money_for_the_paid_invoice_again():
    s = make()
    prep = s.prepare("O2")
    approve(s, prep["token"])
    s.dispatch(prep["execution_id"])  # O2 converted and paid out: the provider balance already reflects it
    s.simulate_deposit("USD", "1000")
    latest = s.state()["plans"][-1]
    assert "O2" not in acts(latest)                                          # satisfied, not planned again
    assert "O2" in acts(s.state()["plans"][0])                               # history keeps it
    assert s.execution(prep["execution_id"])["state"] == "PAYOUT_REPORTED"   # and so does the request


def test_an_obligation_whose_conversion_fails_after_a_replan_comes_back_into_the_plan():
    from dataclasses import replace as _replace
    s = make()
    fake = s.calls._inner if hasattr(s.calls, "_inner") else s.calls
    orig = fake.get_conversion
    status = {"now": "SCHEDULED"}
    fake.get_conversion = lambda cid: _replace(orig(cid), status=status["now"])
    prep = s.prepare("O2")
    approve(s, prep["token"])
    s.dispatch(prep["execution_id"])
    assert s.execution(prep["execution_id"])["state"] == "CONVERTING"
    s.lower_confidence("R1")
    assert "O2" not in acts(s.state()["plans"][-1])        # converting: satisfied for now
    status["now"] = "CANCELLED"
    s.advance(prep["execution_id"])
    assert s.execution(prep["execution_id"])["state"] == "CONVERSION_FAILED"
    s.simulate_deposit("USD", "1000")
    assert "O2" in acts(s.state()["plans"][-1])            # unpaid again: planned again, not lost


def test_a_replan_after_money_moved_uses_fresh_balances():
    s = make()
    prep = s.prepare("O2")
    approve(s, prep["token"])
    s.dispatch(prep["execution_id"])
    s.lower_confidence("R1")
    assert s.plan.inputs.balances["USD"] < s.stages[0].plan.inputs.balances["USD"]   # the conversion's dollars are gone


def test_an_unanswered_request_lapses_at_its_approval_deadline_and_can_be_replaced():
    s = make()
    prep = s.prepare("O2")
    s.skip_clock(61)  # nobody opened the approver page; no approval was attempted
    ex = s.state()["executions"][0]
    assert ex["state"] == "EXPIRED" and ex["counts"] == {"conversion_create": 0, "transfer_create": 0}
    new = s.prepare("O2")  # a replacement is allowed without any refusal having happened first
    assert new["execution_id"] != prep["execution_id"]
    assert s.approver_view(new["token"])["state"] == "live"
    late = approve(s, prep["token"])  # the old link stays refused
    assert late["ok"] is False and late["code"] == "EXPIRED"


def test_an_unanswered_request_lapses_at_the_approval_deadline_while_the_quote_is_still_valid():
    s = make()
    prep = s.prepare("O2")
    s.skip_clock(49)  # one second before the approval deadline (quote 60 s, margin 10 s)
    assert s.state()["executions"][0]["state"] == "AWAITING_APPROVAL"
    s.skip_clock(2)   # 51 s: past the approval deadline, 9 s before the quote itself ends; no approval attempted
    ex = s.state()["executions"][0]
    assert ex["state"] == "EXPIRED" and ex["counts"] == {"conversion_create": 0, "transfer_create": 0}
    new = s.prepare("O2")
    assert new["execution_id"] != prep["execution_id"] and s.approver_view(new["token"])["state"] == "live"


def test_a_tap_inside_the_margin_is_a_closed_approval_window_not_an_expired_quote():
    s = make()
    prep = s.prepare("O2")
    s.skip_clock(55)  # approval closes 10 s before the 60 s quote ends; the quote itself is still valid
    out = approve(s, prep["token"])
    assert out["ok"] is False and out["code"] == "EXPIRED"
    assert "quote" not in out["message"].lower() or "approval" in out["message"].lower()


def test_an_expired_fund_request_speaks_of_its_approval_window_not_a_rate():
    s = make()
    prep = s.prepare("O1")  # FUND: no conversion, no quote
    for _ in range(12):  # past any FUND approval window (skip_clock takes at most 600 s)
        s.skip_clock(600)
    msg = s.approver_view(prep["token"])["message"]
    assert "rate" not in msg.lower() and "approval window closed" in msg.lower() and "nothing was paid" in msg.lower()


def test_a_replan_after_an_uncertain_send_reads_balances_again_and_says_so_when_it_cannot():
    s = make()
    fake = s.calls._inner if hasattr(s.calls, "_inner") else s.calls
    prep = s.prepare("O2")
    approve(s, prep["token"])
    fake.inject("create_conversion", "drop_response")   # the create is sent, its answer is lost: outcome unknown
    s.dispatch(prep["execution_id"])
    assert s.execution(prep["execution_id"])["state"] == "UNKNOWN"
    orig = fake.get_balances
    def broken():
        from rate_locked.awx.adapter import AirwallexError
        raise AirwallexError("unavailable", "balances down", 503)
    fake.get_balances = broken
    s.lower_confidence("R1")
    assert any("could not be read again" in t for t in s.state()["plans"][-1]["trigger"])


def test_a_simulated_deposit_followed_by_a_failed_balance_read_says_the_deposit_went_through():
    from rate_locked.awx.adapter import AirwallexError
    s = make()
    fake = s.calls._inner if hasattr(s.calls, "_inner") else s.calls
    def broken():
        raise AirwallexError("unavailable", "balances down", 503)
    fake.get_balances = broken
    with pytest.raises(SessionError) as e:
        s.simulate_deposit("USD", "1000")
    assert e.value.code == "DEPOSIT_DONE_BALANCES_UNREAD"
    assert "was simulated" in e.value.message and "do not simulate it again" in e.value.message.lower()
    assert [c["op"] for c in s.state()["calls"]].count("simulate_deposit") == 1

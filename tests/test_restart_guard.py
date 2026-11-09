"""F1: state is in memory, so a restart after an uncertain create forgot the request and a fresh
approval would mint a new request id and could pay twice. This is a GUARD, not recovery: the write-ahead events are on
disk, and a new process refuses to ask for a new approval on an obligation whose earlier create was never seen to end.
Resolving it is a person's job (look the request id up at the provider), then clear the log deliberately."""
import json

import pytest

from rate_locked.awx.fake import FakeAirwallex
from rate_locked.awx.adapter import AirwallexError
from rate_locked.clock import ManualClock
from rate_locked.executor import EventLog, Executor, StateError, unresolved_intents
from rate_locked.session import Session, SessionError

from helpers import World


def _crashed_world(tmp_path, oid="O1", fault="drop_response", op="create_transfer"):
    path = tmp_path / "events.jsonl"
    w = World()
    w.executor.log = EventLog(path)
    ex = w.approved(oid)
    w.fake.inject(op, fault)
    w.executor.dispatch(ex.execution_id)
    return w, ex, path


def test_dispatch_events_carry_the_obligation_and_request_ids(tmp_path):
    w, ex, path = _crashed_world(tmp_path)
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    d = next(r for r in rows if r["kind"] == "transfer_dispatching")
    assert d["obligation_id"] == "O1" and d["request_id"] == ex.envelope.transfer_request_id


def test_an_uncertain_create_is_unresolved_on_disk(tmp_path):
    w, ex, path = _crashed_world(tmp_path)
    found = unresolved_intents(path)
    assert list(found) == ["O1"]
    assert found["O1"][0]["request_id"] == ex.envelope.transfer_request_id and found["O1"][0]["stage"] == "payout"


def test_a_completed_run_leaves_nothing_unresolved(tmp_path):
    path = tmp_path / "events.jsonl"
    w = World()
    w.executor.log = EventLog(path)
    ex = w.approved("O2")
    w.executor.dispatch(ex.execution_id)
    assert unresolved_intents(path) == {}


def test_a_refused_create_is_resolved_because_the_provider_said_no(tmp_path):
    path = tmp_path / "events.jsonl"
    w = World()
    w.executor.log = EventLog(path)
    ex = w.approved("O1")

    def refuse(**kw):
        raise AirwallexError("insufficient_funds", "no")
    w.fake.create_transfer = refuse
    w.executor.dispatch(ex.execution_id)
    assert ex.state.value == "PAYOUT_FAILED"
    assert unresolved_intents(path) == {}


def test_a_lookup_that_found_the_request_resolves_it(tmp_path):
    w, ex, path = _crashed_world(tmp_path)
    w.executor.reconcile(ex.execution_id)
    assert unresolved_intents(path) == {}


def test_a_new_process_refuses_a_new_approval_for_that_obligation_only(tmp_path):
    w, ex, path = _crashed_world(tmp_path)
    fresh = World()  # the restart: empty memory, same provider world is simulated by a new fake
    fresh.executor.load_unresolved(path)
    with pytest.raises(StateError) as e:
        fresh.prepare("O1")
    assert ex.envelope.transfer_request_id in str(e.value) and "look" in str(e.value).lower()
    assert fresh.prepare("O2")  # other obligations are not blocked


def test_an_unreadable_log_fails_closed(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text('{"kind": "transfer_dispatching"\nnot json\n', encoding="utf-8")
    with pytest.raises(StateError):
        unresolved_intents(path)


def test_a_missing_log_means_nothing_is_unresolved(tmp_path):
    assert unresolved_intents(tmp_path / "nope.jsonl") == {}


def test_the_session_takes_an_event_log_path_and_refuses_on_restart(tmp_path):
    from rate_locked.scenario import NOW
    from helpers import resolve_review
    path = tmp_path / "events.jsonl"

    def session():
        clock = ManualClock(NOW)
        fake = FakeAirwallex.demo(clock=clock)
        s = Session(fake, world="FAKE", clock=clock, fake_controls=fake, run_id="r-test", commit="abc",
                    event_log_path=str(path))
        resolve_review(s)
        return s, fake

    s1, fake1 = session()
    out = s1.prepare("O1")
    view = s1.approver_view(out["token"])
    s1.approver_approve(out["token"], view["digest"], view["digest"][:8])
    fake1.inject("create_transfer", "drop_response")
    s1.dispatch(out["execution_id"])
    assert s1.execution(out["execution_id"])["state"] == "UNKNOWN"
    s2, _ = session()  # the restart
    with pytest.raises(SessionError) as e:
        s2.prepare("O1")
    assert e.value.code == "LIVE_REQUEST_EXISTS" and out["execution_id"] and "request" in e.value.message.lower()
    assert s2.prepare("O2")["token"]


def test_event_log_lines_are_flushed_to_disk_before_the_call(tmp_path):
    path = tmp_path / "events.jsonl"
    w = World()
    w.executor.log = EventLog(path)
    ex = w.approved("O1")
    seen = []
    real = w.fake.create_transfer

    def spy(**kw):
        seen.append("transfer_dispatching" in path.read_text())  # the intent is on disk when the call is made
        return real(**kw)
    w.fake.create_transfer = spy
    w.executor.dispatch(ex.execution_id)
    assert seen == [True]

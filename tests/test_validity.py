"""The approver may choose how long the locked quote stays valid (MIN_1 for the video, MIN_15 or HR_1 for a human
who needs time). The approval window follows the quote, never the other way round, and a new quote after a lapse
asks for the same validity again. MIN_1 stays the default."""
from datetime import timedelta

import pytest

from rate_locked.approval import DEFAULT_MARGIN
from rate_locked.executor import State
from rate_locked.session import SessionError

from helpers import World
from test_session import make


@pytest.mark.parametrize("validity,seconds", [("MIN_1", 60), ("MIN_15", 900), ("HR_1", 3600)])
def test_the_approval_window_follows_the_chosen_quote_validity(validity, seconds):
    w = World()
    prep = w.prepare("O2", validity=validity)
    env, q = prep.execution.envelope, prep.execution.envelope.quote
    assert q.valid_to_at - q.valid_from_at == timedelta(seconds=seconds)
    assert env.expires_at == q.valid_to_at - DEFAULT_MARGIN  # the quote is the bound, not the 10 minute default


def test_a_fifteen_minute_quote_can_still_be_approved_and_sent_after_ten_minutes():
    w = World()
    prep = w.prepare("O2", validity="MIN_15")
    w.clock.advance(11 * 60)
    out = w.executor.approve(prep.token, prep.execution.envelope.digest, "approver-1")
    assert out.ok, out
    ex = w.executor.dispatch(prep.execution.execution_id)
    assert ex.state is State.PAYOUT_REPORTED
    assert w.executor.call_counts(ex.execution_id) == {"conversion_create": 1, "transfer_create": 1}


def test_a_requote_asks_for_the_same_validity_again():
    w = World()
    prep = w.prepare("O2", validity="MIN_15")
    w.clock.advance(15 * 60 + 1)
    out = w.executor.approve(prep.token, prep.execution.envelope.digest, "approver-1")
    assert not out.ok and out.execution.state is State.EXPIRED
    new, _diff = w.executor.requote(prep.execution.execution_id)
    q = new.execution.envelope.quote
    assert q.valid_to_at - q.valid_from_at == timedelta(seconds=900)


def test_session_prepare_takes_a_validity_and_refuses_unknown_ones():
    s = make()
    out = s.prepare("O2", validity="HR_1")
    q = s.execution(out["execution_id"])["quote"]
    assert q["valid_to_at"] > q["valid_from_at"]
    with pytest.raises(SessionError) as e:
        s.prepare("O1", validity="MIN_2")
    assert e.value.code == "BAD_VALIDITY" and e.value.status == 400


def test_session_default_stays_one_minute():
    s = make()
    out = s.prepare("O2")
    view = s.approver_view(out["token"])
    from datetime import datetime
    a, b = (datetime.fromisoformat(view[k].replace("Z", "+00:00")) for k in ("valid_from_at", "valid_to_at"))
    assert (b - a).total_seconds() == 60


def test_the_approver_view_and_the_console_show_the_fee_and_method():
    s = make()
    view = s.approver_view(s.prepare("O2")["token"])
    assert view["fee"] == "0.00" and view["payout_method"] == "LOCAL"
    ex = s.execution(s.prepare("O1")["execution_id"])
    assert ex["payout"]["fee"] == "0.00" and ex["payout"]["method"] == "LOCAL"


from test_server import Console, srv  # noqa: E402,F401  (the loopback console fixture)


def test_api_prepare_passes_the_validity_and_bad_values_are_a_400(srv):  # noqa: F811
    c = Console(srv)
    status, out = c.post("/api/prepare", {"obligation_id": "O2", "validity": "MIN_15"})
    assert status == 200
    _, ex = c.post("/api/execution", {"execution_id": out["execution_id"]})
    from datetime import datetime
    a, b = (datetime.fromisoformat(ex["quote"][k].replace("Z", "+00:00")) for k in ("valid_from_at", "valid_to_at"))
    assert (b - a).total_seconds() == 900
    status, err = c.post("/api/prepare", {"obligation_id": "O1", "validity": "MIN_2"})
    assert status == 400 and err.get("error") == "BAD_VALIDITY"

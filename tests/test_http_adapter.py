"""The HTTP adapter against the local fake server: passes the same contract, plus transport rules."""
from decimal import Decimal as D

import pytest

from rate_locked.awx.adapter import AirwallexAdapter, AirwallexError, UncertainOutcome
from rate_locked.awx.fake import FakeAirwallex
from rate_locked.awx.fake_server import FakeServer
from rate_locked.awx.http_adapter import HttpAdapter, check_host
from rate_locked.awx import wire

from adapter_contract import AdapterContract


class _Ctl:
    """Control handle: the FakeAirwallex plus the server it sits behind."""

    def __init__(self, fake, server):
        self.fake, self.server = fake, server

    def __getattr__(self, name):
        return getattr(self.fake, name)

    def __setattr__(self, name, value):
        if name in ("fake", "server"):
            object.__setattr__(self, name, value)
        else:
            setattr(self.fake, name, value)

    def close(self):
        self.server.close()


class TestHttpAdapterContract(AdapterContract):
    def make(self):
        fake = FakeAirwallex.demo()
        server = FakeServer(fake).start()
        adapter = HttpAdapter(server.base_url, "client-id-for-tests", "key-for-tests", timeout=5, clock=fake.clock)
        return adapter, _Ctl(fake, server)


@pytest.fixture
def served():
    fake = FakeAirwallex.demo()
    server = FakeServer(fake, credentials=("good-id", "good-key")).start()
    yield fake, server
    server.close()


def test_http_adapter_satisfies_the_protocol(served):
    _fake, server = served
    assert isinstance(HttpAdapter(server.base_url, "good-id", "good-key"), AirwallexAdapter)


def test_bad_credentials_raise_a_provider_error(served):
    _fake, server = served
    a = HttpAdapter(server.base_url, "bad", "bad")
    with pytest.raises(AirwallexError) as e:
        a.get_balances()
    assert e.value.code == "credentials_invalid"


def test_token_is_refreshed_after_expiry(served):
    fake, server = served
    a = HttpAdapter(server.base_url, "good-id", "good-key", clock=fake.clock)
    a.get_balances()
    fake.advance(31 * 60)
    a.get_balances()  # would be 401 without a refresh
    logins = [r for r in server.requests if r[1].endswith("/authentication/login")]
    assert len(logins) == 2


def test_adapter_never_sends_x_api_version_on_fx_calls(served):
    fake, server = served
    a = HttpAdapter(server.base_url, "good-id", "good-key")
    a.create_quote(buy_currency="GBP", sell_currency="USD", validity="MIN_1", buy_amount=D("10"))  # fake rejects the header


def test_a_create_is_never_resent_after_a_lost_response(served):
    fake, server = served
    a = HttpAdapter(server.base_url, "good-id", "good-key", clock=fake.clock)
    q = a.create_quote(buy_currency="GBP", sell_currency="USD", validity="MIN_1", buy_amount=D("1000"))
    fake.inject("create_conversion", "drop_response")
    with pytest.raises(UncertainOutcome):
        a.create_conversion(request_id="r1", quote_id=q.quote_id, buy_currency="GBP", sell_currency="USD",
                            buy_amount=D("1000"))
    posts = [r for r in server.requests if r == ("POST", "/api/v1/fx/conversions/create")]
    assert len(posts) == 1


def test_host_lock():
    assert check_host("https://api.sandbox.airwallex.com") == "https://api.sandbox.airwallex.com"
    assert check_host("http://127.0.0.1:9999") == "http://127.0.0.1:9999"
    for bad in ("https://api.airwallex.com", "https://evil.example", "http://api.sandbox.airwallex.com",
                "https://api.sandbox.airwallex.com.evil.example"):
        with pytest.raises(ValueError):
            check_host(bad)


def test_repr_never_shows_credentials():
    a = HttpAdapter("http://127.0.0.1:1", "secret-id", "secret-key")
    assert "secret" not in repr(a)


def test_wire_timestamps_round_trip_with_and_without_colon():
    for s in ("2026-11-02T09:00:00+0000", "2026-11-02T09:00:00Z", "2026-11-02T09:00:00+00:00"):
        assert wire.parse_ts(s).isoformat() == "2026-11-02T09:00:00+00:00"


def test_amounts_parse_as_decimal_not_float():
    data = wire.loads('{"a": 1306.50, "b": 1000}')
    assert data["a"] == D("1306.50") and isinstance(data["a"], D)


def test_a_read_survives_one_dropped_connection_but_a_create_is_never_resent():
    """The live sandbox sometimes drops the first connection (RemoteDisconnected). Reads and login try once more;
    a create must stay a single send (the outcome is then looked up, never re-sent)."""
    import http.client
    fake = FakeAirwallex.demo()
    server = FakeServer(fake).start()
    try:
        a = HttpAdapter(server.base_url, "client-id-for-tests", "key-for-tests", timeout=5, clock=fake.clock)
        real_open, sent = a._opener.open, []

        def flaky(req, timeout=None):
            sent.append(req.get_method() + req.full_url.rsplit("/", 1)[-1])
            if len(sent) in (1, 4):  # the first login attempt, then the first create attempt
                raise http.client.RemoteDisconnected("dropped")
            return real_open(req, timeout=timeout)

        a._opener.open = flaky
        assert {b.currency for b in a.get_balances()} >= {"USD"}  # login retried once, balances answered
        n = len(sent)
        with pytest.raises(UncertainOutcome):
            a.create_conversion(request_id="r-1", quote_id="q", buy_currency="GBP", sell_currency="USD",
                                buy_amount=D("1"))
        assert len(sent) == n + 1  # exactly one attempt
    finally:
        server.close()

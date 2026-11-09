import importlib.util
import json
import sys
from decimal import Decimal as D
from pathlib import Path

import pytest

from rate_locked.awx.fake import FakeAirwallex
from rate_locked.awx.fake_server import FakeServer

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("sandbox_probe", ROOT / "scripts" / "sandbox_probe.py")
probe = importlib.util.module_from_spec(spec)
sys.modules["sandbox_probe"] = probe
spec.loader.exec_module(probe)

CID, KEY = "client-id-SENTINEL-1234", "api-key-SENTINEL-abcdefghijkl"


@pytest.fixture
def served(monkeypatch):
    fake = FakeAirwallex.demo()
    server = FakeServer(fake).start()
    monkeypatch.setenv("AIRWALLEX_CLIENT_ID", CID)
    monkeypatch.setenv("AIRWALLEX_API_KEY", KEY)
    yield fake, server
    server.close()


def test_default_run_is_read_only_plus_quotes_and_never_leaks_keys(served, tmp_path, capsys):
    fake, server = served
    code = probe.main(["--base-url", server.base_url, "--out", str(tmp_path)])
    out = capsys.readouterr().out
    assert code == 0
    assert CID not in out and KEY not in out
    files = list(tmp_path.glob("probe_*.json"))
    assert len(files) == 1
    text = files[0].read_text()
    assert CID not in text and KEY not in text
    ev = json.loads(text)
    ids = [s["id"] for s in ev["steps"]]
    assert {"P1", "P2", "P3", "P4", "P4b", "P5", "P6", "P10"} <= set(ids)
    assert "P7" not in ids  # no conversion without --convert
    assert not any(c for c in server.requests if "conversions/create" in c[1] or "transfers/create" in c[1])
    assert "Quote works" in ev["decision_hint"]
    # both schema paths were read for each corridor
    paths = [s["path"] for s in ev["steps"] if s["id"] == "P4"]
    assert any(p.endswith("/beneficiaries/schema") for p in paths) and any(p.endswith("/generate") for p in paths)


def test_token_never_appears_in_evidence(served, tmp_path):
    fake, server = served
    probe.main(["--base-url", server.base_url, "--out", str(tmp_path)])
    tok = next(iter(server.tokens))
    assert tok not in next(tmp_path.glob("probe_*.json")).read_text()


def test_convert_expiry_and_deposit_steps(served, tmp_path, capsys):
    fake, server = served
    code = probe.main(["--base-url", server.base_url, "--out", str(tmp_path), "--convert", "--expiry-test",
                       "--simulate-deposit"], sleep=lambda s: fake.advance(s))
    assert code == 0
    ev = json.loads(next(tmp_path.glob("probe_*.json")).read_text())
    by = {}
    for s in ev["steps"]:
        by.setdefault(s["id"], []).append(s)
    assert by["P7"][0]["ok"] and by["P7"][0]["rate_equals_quote"] is True
    assert len(by["P9"]) == 4 and "items found: 1" in by["P9"][-1]["note"]
    p8 = by["P8"][-1]
    assert p8["ok"] and "refused as expected" in p8["note"] and p8["response"]["code"] == "expired_quote"
    assert by["SIM"][0]["ok"]
    assert ev["decision_hint"].startswith("F0")
    assert len(fake._conversions) == 1  # exactly one conversion exists at the provider
    assert sum(1 for r in server.requests if r[1] == "/api/v1/fx/conversions/create") == 2  # P7 plus the P8 attempt


def test_missing_keys_print_help_and_exit_2(monkeypatch, capsys):
    monkeypatch.delenv("AIRWALLEX_CLIENT_ID", raising=False)
    monkeypatch.delenv("AIRWALLEX_API_KEY", raising=False)
    assert probe.main([]) == 2
    out = capsys.readouterr().out
    assert "Read-Host" in out and "AIRWALLEX_API_KEY" in out


def test_bad_login_stops_and_returns_1(tmp_path, monkeypatch, capsys):
    server = FakeServer(FakeAirwallex.demo(), credentials=("x", "y")).start()
    try:
        monkeypatch.setenv("AIRWALLEX_CLIENT_ID", CID)
        monkeypatch.setenv("AIRWALLEX_API_KEY", KEY)
        assert probe.main(["--base-url", server.base_url, "--out", str(tmp_path)]) == 1
        out = capsys.readouterr().out
        assert "P1" in out and "STOP-CHECK" in out and KEY not in out
    finally:
        server.close()


def test_dry_run_needs_no_keys_and_makes_no_call(monkeypatch, capsys):
    monkeypatch.delenv("AIRWALLEX_API_KEY", raising=False)
    assert probe.main(["--dry-run", "--convert"]) == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out and "P7" in out and "api.sandbox.airwallex.com" in out


@pytest.mark.parametrize("bad", ["https://api.airwallex.com", "https://example.com", "http://api.sandbox.airwallex.com",
                                 "https://api.sandbox.airwallex.com.evil.test"])
def test_host_lock(bad, capsys):
    assert probe.main(["--base-url", bad, "--dry-run"]) == 2
    with pytest.raises(probe.HostError):
        probe.check_base(bad)


def test_sanitize_redacts_secrets_masks_accounts_and_truncates():
    data = {"token": "abc", "api_key": "k", "nested": {"Authorization": "Bearer z", "iban": "GB29NWBK60161331926819"},
            "note": "contains SECRETVALUE inside", "items": list(range(30)), "long": "x" * 400, "amount": D("1.50")}
    out = probe.sanitize(data, secrets=("SECRETVALUE",))
    assert out["token"] == "[redacted]" and out["api_key"] == "[redacted]" and out["nested"]["Authorization"] == "[redacted]"
    assert out["nested"]["iban"] == "****6819"
    assert "SECRETVALUE" not in out["note"] and "[redacted]" in out["note"]
    assert len(out["items"]) == 21 and out["long"].endswith("...[truncated]") and out["amount"] == "1.50"


def test_sse_and_json_rpc_parsing():
    msg = {"jsonrpc": "2.0", "id": 1, "result": {"serverInfo": {"name": "docs"}}}
    assert probe.parse_sse_or_json(json.dumps(msg)) == msg
    assert probe.parse_sse_or_json("event: message\ndata: " + json.dumps(msg) + "\n\n") == msg
    with pytest.raises(ValueError):
        probe.parse_sse_or_json("nothing here")


def test_probe_never_sends_an_api_version_header():
    src = (ROOT / "scripts" / "sandbox_probe.py").read_text()
    assert '"x-api-version"' not in src and "'x-api-version'" not in src


# ---- review fixes F11 to F14 -----------------------------------------------------------------------------

def _scripted_http(script):
    """Replace probe.http with a function answering from `script(method, url, headers, body)`."""
    def fake(method, url, *, headers=None, body=None, timeout=20):
        return script(method, url, headers or {}, body)
    return fake


def test_f11_echoed_secrets_never_reach_stdout_or_evidence(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("AIRWALLEX_CLIENT_ID", CID)
    monkeypatch.setenv("AIRWALLEX_API_KEY", KEY)
    tok = "TOKSENT-0123456789"

    def script(method, url, headers, body):
        if url.endswith("/authentication/login"):
            return 201, {"token": tok, "expires_at": KEY}, {"x-api-version": CID}
        if url.endswith("/balances/current"):
            return 200, [{"currency": KEY, "available_amount": 0}], {"x-api-version": tok}
        return 200, {KEY: tok, "client_rate": "1.3", "quote_id": CID, "valid_to_at": tok}, {"x-api-version": KEY}

    monkeypatch.setattr(probe, "http", _scripted_http(script))
    probe.main(["--base-url", "http://127.0.0.1:9", "--out", str(tmp_path)])
    out = capsys.readouterr().out
    text = next(tmp_path.glob("probe_*.json")).read_text()
    for secret in (CID, KEY, tok):
        assert secret not in out and secret not in text, secret


def test_f12_no_base_override_and_origin_checks_on_every_request():
    import inspect
    assert "base" not in inspect.signature(probe.Probe.call).parameters
    with pytest.raises(probe.HostError):
        probe.http("GET", "https://example.invalid/x", headers={"Authorization": "Bearer x"})
    for bad in ("http://127.0.0.1:1@evil.test", "http://127.0.0.1:80/path", "http://127.0.0.1?x=1",
                "http://127.0.0.1#f", "https://api.sandbox.airwallex.com/extra"):
        with pytest.raises(probe.HostError):
            probe.check_base(bad)


def test_f13_uncertain_conversion_is_looked_up_and_intent_is_saved_first(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("AIRWALLEX_CLIENT_ID", CID)
    monkeypatch.setenv("AIRWALLEX_API_KEY", KEY)
    lookups = []

    def script(method, url, headers, body):
        if url.endswith("/authentication/login"):
            return 201, {"token": "t-123456789", "expires_at": "2999-01-01T00:00:00+0000"}, {}
        if url.endswith("/fx/quotes/create"):
            return 201, {"quote_id": "q1", "client_rate": "1.3", "usage": "SINGLE_USE", "valid_from_at": "a", "valid_to_at": "b"}, {}
        if url.endswith("/fx/conversions/create"):
            return 0, {"_transport_error": "timeout"}, {}
        if "/fx/conversions?" in url:
            lookups.append(url)
            return 200, {"items": []}, {}
        return 200, {}, {}

    monkeypatch.setattr(probe, "http", _scripted_http(script))
    code = probe.main(["--base-url", "http://127.0.0.1:9", "--out", str(tmp_path), "--convert"], sleep=lambda s: None)
    out = capsys.readouterr().out
    assert len(lookups) == 4 and "UNCERTAIN" in out and code == 3


def test_f13_intent_is_on_disk_before_a_financial_call(monkeypatch, tmp_path):
    monkeypatch.setenv("AIRWALLEX_CLIENT_ID", CID)
    monkeypatch.setenv("AIRWALLEX_API_KEY", KEY)

    def script(method, url, headers, body):
        if url.endswith("/authentication/login"):
            return 201, {"token": "t-123456789", "expires_at": "x"}, {}
        if url.endswith("/fx/quotes/create"):
            return 201, {"quote_id": "q1", "client_rate": "1.3"}, {}
        if url.endswith("/fx/conversions/create"):
            files = list(tmp_path.glob("probe_*.json"))
            assert files, "evidence must exist before the conversion is sent"
            ev = json.loads(files[0].read_text())
            assert any(i["op"] == "conversion" and i["request_id"] == body["request_id"] for i in ev["intents"])
            raise KeyboardInterrupt
        return 200, {}, {}

    monkeypatch.setattr(probe, "http", _scripted_http(script))
    with pytest.raises(KeyboardInterrupt):
        probe.main(["--base-url", "http://127.0.0.1:9", "--out", str(tmp_path), "--convert"])


def test_f13_expiry_test_needs_the_specific_refusal_code(monkeypatch, tmp_path):
    monkeypatch.setenv("AIRWALLEX_CLIENT_ID", CID)
    monkeypatch.setenv("AIRWALLEX_API_KEY", KEY)

    def script(method, url, headers, body):
        if url.endswith("/authentication/login"):
            return 201, {"token": "t-123456789", "expires_at": "x"}, {}
        if url.endswith("/fx/quotes/create"):
            return 201, {"quote_id": "q1", "client_rate": "1.3"}, {}
        if url.endswith("/fx/conversions/create"):
            return 503, {"code": "service_unavailable"}, {}
        return 200, {}, {}

    monkeypatch.setattr(probe, "http", _scripted_http(script))
    probe.main(["--base-url", "http://127.0.0.1:9", "--out", str(tmp_path), "--expiry-test"], sleep=lambda s: None)
    ev = json.loads(next(tmp_path.glob("probe_*.json")).read_text())
    p8 = [s for s in ev["steps"] if s["id"] == "P8"][-1]
    assert p8["ok"] is False


def test_f14_probe_encodes_decimals_exactly():
    text = probe.encode({"a": D("90071992547409.91"), "b": [D("1.30")]})
    back = json.loads(text, parse_float=D)
    assert back["a"] == D("90071992547409.91") and back["b"][0] == D("1.30")
    with pytest.raises(ValueError):
        probe.encode({"a": D("Infinity")})

"""Web console over HTTP on loopback: host and origin checks, a CSRF token per console session, approver links
kept apart from the console, and the full story driven through the JSON API on the local fake."""
import http.client
import json
import re

import pytest

from rate_locked.clock import ManualClock
from rate_locked.scenario import NOW
from rate_locked.server import ConsoleServer, build_fake_session

from helpers import REVIEW, resolve_review


def _server(resolve):
    session = build_fake_session(clock=ManualClock(NOW), run_id="r-test", commit="abc1234")
    if resolve:
        resolve_review(session)  # most tests start after the person has resolved the one review flag
    return ConsoleServer(session).start()


@pytest.fixture()
def srv():
    s = _server(resolve=True)
    yield s
    s.close()


@pytest.fixture()
def fresh():
    s = _server(resolve=False)
    yield s
    s.close()


def raw(srv, method, path, body=None, headers=None, host=None):
    conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=5)
    h = {"Host": host or f"127.0.0.1:{srv.port}"}
    h.update(headers or {})
    data = None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
    if data is not None:
        h.setdefault("Content-Type", "application/json")
    conn.request(method, path, body=data, headers=h)
    r = conn.getresponse()
    out = r.status, {k.lower(): v for k, v in r.getheaders()}, r.read()
    conn.close()
    return out


class Console:
    def __init__(self, srv):
        self.srv = srv
        status, headers, body = raw(srv, "GET", "/")
        assert status == 200
        self.cookie = headers["set-cookie"].split(";", 1)[0]
        self.csrf = re.search(rb'<meta name="csrf-token" content="([^"]+)">', body).group(1).decode()
        self.origin = f"http://127.0.0.1:{srv.port}"

    def h(self):
        return {"Cookie": self.cookie, "X-CSRF-Token": self.csrf, "Origin": self.origin}

    def get(self, path):
        status, _, body = raw(self.srv, "GET", path, headers=self.h())
        return status, json.loads(body)

    def post(self, path, body=None):
        status, _, out = raw(self.srv, "POST", path, body or {}, headers=self.h())
        return status, json.loads(out)


def approver_post(srv, token, action, body):
    status, _, out = raw(srv, "POST", f"/api/approver/{token}/{action}", body,
                         headers={"Origin": f"http://127.0.0.1:{srv.port}"})
    return status, json.loads(out)


def test_server_binds_loopback_only():
    with pytest.raises(ValueError):
        ConsoleServer(build_fake_session(), host="0.0.0.0")


def test_console_page_headers_and_cookie(srv):
    status, headers, body = raw(srv, "GET", "/")
    assert status == 200 and headers["content-type"].startswith("text/html")
    assert "HttpOnly" in headers["set-cookie"] and "SameSite=Strict" in headers["set-cookie"]
    csp = headers["content-security-policy"]
    assert "default-src 'self'" in csp and "script-src 'self'" in csp and "frame-ancestors 'self'" in csp
    assert headers["referrer-policy"] == "no-referrer" and headers["x-content-type-options"] == "nosniff"
    assert headers["cache-control"] == "no-store"
    assert b"__CSRF__" not in body


def test_dns_rebinding_host_is_refused(srv):
    status, _, _ = raw(srv, "GET", "/", host=f"attacker.example:{srv.port}")
    assert status == 403
    status, _, _ = raw(srv, "GET", "/", host=f"localhost:{srv.port}")
    assert status == 200


def test_state_needs_the_console_cookie_and_csrf(srv):
    c = Console(srv)
    assert c.get("/api/state")[0] == 200
    assert raw(srv, "GET", "/api/state")[0] == 403
    assert raw(srv, "GET", "/api/state", headers={"Cookie": c.cookie})[0] == 403
    assert raw(srv, "GET", "/api/state", headers={"Cookie": c.cookie, "X-CSRF-Token": "x" * 40})[0] == 403


def test_console_posts_need_origin_cookie_and_csrf(srv):
    c = Console(srv)
    body = {"receipt_id": "R1"}
    assert raw(srv, "POST", "/api/lower-confidence", body, headers={"Cookie": c.cookie, "X-CSRF-Token": c.csrf})[0] == 403
    bad_origin = {**c.h(), "Origin": "http://attacker.example"}
    assert raw(srv, "POST", "/api/lower-confidence", body, headers=bad_origin)[0] == 403
    no_csrf = {"Cookie": c.cookie, "Origin": c.origin}
    assert raw(srv, "POST", "/api/lower-confidence", body, headers=no_csrf)[0] == 403
    other = Console(srv)  # a second console session has its own token
    mixed = {"Cookie": c.cookie, "X-CSRF-Token": other.csrf, "Origin": c.origin}
    assert raw(srv, "POST", "/api/lower-confidence", body, headers=mixed)[0] == 403
    assert c.post("/api/lower-confidence", body)[0] == 200


def test_approver_cannot_plan_or_dispatch(srv):
    c = Console(srv)
    _, prep = c.post("/api/prepare", {"obligation_id": "O2"})
    tok = prep["token"]
    status, headers, page = raw(srv, "GET", f"/approve/{tok}")
    assert status == 200 and "set-cookie" not in headers and headers["referrer-policy"] == "no-referrer"
    origin = {"Origin": c.origin}
    for path in ("/api/dispatch", "/api/prepare", "/api/lower-confidence", "/api/state"):
        method = "GET" if path == "/api/state" else "POST"
        assert raw(srv, method, path, {"execution_id": prep["execution_id"]} if method == "POST" else None,
                   headers=origin)[0] == 403
    assert approver_post(srv, tok, "dispatch", {})[0] == 404
    status, view = raw(srv, "GET", f"/api/approver/{tok}")[0], json.loads(raw(srv, "GET", f"/api/approver/{tok}")[2])
    assert status == 200 and "token" not in json.dumps(view)


def test_approver_post_needs_origin(srv):
    c = Console(srv)
    _, prep = c.post("/api/prepare", {"obligation_id": "O2"})
    status, _, _ = raw(srv, "POST", f"/api/approver/{prep['token']}/deny", {})
    assert status == 403


def test_full_story_over_http(srv):
    c = Console(srv)
    assert c.post("/api/lower-confidence", {"receipt_id": "R1"})[0] == 200
    assert c.post("/api/deposit", {"currency": "USD", "amount": "1000"})[0] == 200
    _, st = c.get("/api/state")
    assert [p["version"] for p in st["plans"]] == [1, 2, 3]
    _, prep = c.post("/api/prepare", {"obligation_id": "O2"})
    tok = prep["token"]
    view = json.loads(raw(srv, "GET", f"/api/approver/{tok}")[2])
    assert view["state"] == "live"
    assert c.post("/api/control/skip-clock", {"seconds": 61})[0] == 200
    status, out = approver_post(srv, tok, "approve", {"digest": view["digest"], "typed": view["digest"][:8]})
    assert status == 200 and out["code"] == "EXPIRED"
    _, ex = c.post("/api/execution", {"execution_id": prep["execution_id"]})
    assert ex["counts"] == {"conversion_create": 0, "transfer_create": 0}
    c.post("/api/control/fake-rate", {"pair": "GBPUSD", "rate": "1.31"})
    _, new = c.post("/api/requote", {"execution_id": prep["execution_id"]})
    assert new["difference"]["text"] == "Difference: USD 10.00 more"
    v2 = json.loads(raw(srv, "GET", f"/api/approver/{new['token']}")[2])
    status, out = approver_post(srv, new["token"], "approve", {"digest": v2["digest"], "typed": v2["digest"][:8]})
    assert out["ok"] is True
    _, ex = c.post("/api/dispatch", {"execution_id": new["execution_id"]})
    assert ex["transfer"]["status"] == "PROCESSING"
    _, ex = c.post("/api/transition", {"execution_id": new["execution_id"], "status": "PAID"})
    assert ex["state"] == "PAID"
    _, rec = c.post("/api/record", {"execution_id": new["execution_id"]})
    assert rec["all_match"] is True
    _, ver = c.post("/api/verify", {"bundle": rec})
    assert ver["verdict"] == "PASS"
    rec["rows"][3]["executed"] = "9.99"
    _, ver = c.post("/api/verify", {"bundle": rec})
    assert ver["verdict"] == "FAIL"
    old = json.loads(raw(srv, "GET", f"/api/approver/{new['token']}")[2])
    assert old["state"] == "used" and old["message"] == "This approval link was already used."


def test_errors_are_json_with_status(srv):
    c = Console(srv)
    status, out = c.post("/api/prepare", {"obligation_id": "O9"})
    assert status == 404 and out["error"] == "UNKNOWN_OBLIGATION"
    status, _, body = raw(srv, "POST", "/api/prepare", b"{not json", headers=c.h())
    assert status == 400
    status, _, _ = raw(srv, "POST", "/api/verify", b"x" * 300_000, headers=c.h())
    assert status == 413
    assert raw(srv, "GET", "/api/approver/nope")[0] == 404


def test_static_files_are_whitelisted(srv):
    assert raw(srv, "GET", "/static/app.css")[0] == 200
    assert raw(srv, "GET", "/static/console.js")[0] == 200
    assert raw(srv, "GET", "/static/approver.js")[0] == 200
    assert raw(srv, "GET", "/static/theme.js")[0] == 200
    for path in ("/static/../server.py", "/static/%2e%2e/server.py", "/static/nope.js", "/server.py"):
        assert raw(srv, "GET", path)[0] == 404, path


def test_review_is_resolved_on_the_console_before_the_plan_proceeds(fresh):
    c = Console(fresh)
    _, st = c.get("/api/state")
    assert st["plans"] == [] and st["review"]["pending"] == ["inv-initech"]
    status, out = c.post("/api/prepare", {"obligation_id": "O1"})
    assert status == 409 and out["error"] == "REVIEW_PENDING"
    status, out = c.post("/api/review/resolve", {**REVIEW, "note": ""})
    assert status == 400 and out["error"] == "NOTE_REQUIRED"
    origin_only = {"Origin": c.origin}  # the approver page holds no console session
    assert raw(fresh, "POST", "/api/review/resolve", REVIEW, headers=origin_only)[0] == 403
    status, out = c.post("/api/review/resolve", REVIEW)
    assert status == 200 and out["ready"] is True and out["plan_version"] == 1
    _, st = c.get("/api/state")
    assert [p["version"] for p in st["plans"]] == [1] and st["review"]["pending"] == []
    assert c.post("/api/prepare", {"obligation_id": "O2"})[0] == 200


def test_second_console_tab_keeps_the_first_tab_working(srv):
    first = Console(srv)
    # a second tab of the same browser sends the existing cookie when it loads "/"
    status, headers, body = raw(srv, "GET", "/", headers={"Cookie": first.cookie})
    assert status == 200
    second_csrf = re.search(rb'<meta name="csrf-token" content="([^"]+)">', body).group(1).decode()
    assert second_csrf == first.csrf
    assert first.get("/api/state")[0] == 200
    # a browser with no cookie still gets its own console
    other = Console(srv)
    assert other.cookie != first.cookie and other.csrf != first.csrf

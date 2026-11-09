"""Web console on loopback: `python -m rate_locked serve`, then open http://127.0.0.1:8770/

Standard library only (http.server). Static HTML, CSS and vanilla JS from ./web, JSON endpoints over one Session.

Security, all checked by tests:
  * binds 127.0.0.1 only; the Host header must name 127.0.0.1 or localhost on this port (DNS rebinding);
  * every POST must carry a matching Origin header;
  * the console page sets an HttpOnly, SameSite=Strict session cookie and embeds a CSRF token for that session;
    every console API call needs both;
  * approver links carry their own single-use token and reach only the approver endpoints (view, approve, deny);
    the approver page cannot plan, dispatch or read the console state;
  * CSP allows only this origin (no inline script or style), no referrer is sent, nothing is cached.
"""
from __future__ import annotations

import json
import re
import secrets
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from .awx.fake import FakeAirwallex
from .clock import RunningClock
from .scenario import NOW
from .session import Session, SessionError

WEB = Path(__file__).resolve().parent / "web"
STATIC = {"app.css": "text/css; charset=utf-8", "console.js": "text/javascript; charset=utf-8",
          "approver.js": "text/javascript; charset=utf-8",
          "theme.js": "text/javascript; charset=utf-8"}
MAX_BODY = 256 * 1024
COOKIE = "rl_console"
DEFAULT_PORT = 8770
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
       "font-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'self'")
TOKEN_RX = re.compile(r"^[A-Za-z0-9_-]{16,128}$")


def git_commit(root: Optional[Path] = None) -> str:
    root = root or Path(__file__).resolve().parents[2]
    try:
        head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=root, capture_output=True, text=True,
                              timeout=5).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True,
                               timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "uncommitted"
    if not head:
        return "uncommitted"
    return head + ("-dirty" if dirty else "")


def build_fake_session(*, clock=None, run_id: Optional[str] = None, commit: Optional[str] = None, reader=None) -> Session:
    """The FAKE world: the local fake of the Airwallex endpoints, test controls on, no network."""
    clock = clock or RunningClock(NOW)
    fake = FakeAirwallex.demo(clock=clock)
    return Session(fake, world="FAKE", clock=clock, fake_controls=fake, run_id=run_id, reader=reader,
                   commit=commit if commit is not None else git_commit())


class _Reject(Exception):
    def __init__(self, status: int, code: str, message: str = ""):
        super().__init__(code)
        self.status, self.code, self.message = status, code, message or code


class ConsoleServer:
    def __init__(self, session: Session, host: str = "127.0.0.1", port: int = 0):
        if host != "127.0.0.1":
            raise ValueError("the console binds 127.0.0.1 only")
        self.session = session
        self.consoles: dict = {}  # session cookie -> CSRF token
        self._lock = threading.Lock()
        outer = self

        class Handler(_Handler):
            server_ref = outer

        self._httpd = ThreadingHTTPServer((host, port), Handler)
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=lambda: self._httpd.serve_forever(poll_interval=0.05), daemon=True)

    @property
    def port(self) -> int:
        return self._httpd.server_address[1]

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> "ConsoleServer":
        self._thread.start()
        return self

    def serve_forever(self) -> None:
        self._httpd.serve_forever(poll_interval=0.2)

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()

    def new_console(self) -> tuple:
        sid, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with self._lock:
            self.consoles[sid] = csrf
        return sid, csrf

    def csrf_for(self, sid: str) -> Optional[str]:
        with self._lock:
            return self.consoles.get(sid)


class _Handler(BaseHTTPRequestHandler):
    server_ref: ConsoleServer
    protocol_version = "HTTP/1.1"
    server_version = "RateLocked"
    sys_version = ""

    def log_message(self, *args):  # quiet
        pass

    # ---- responses ---------------------------------------------------------------------------------
    def _headers(self, status: int, ctype: str, length: int, extra: Optional[dict] = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()

    def _json(self, status: int, payload) -> None:
        body = json.dumps(payload, sort_keys=True).encode()
        self._headers(status, "application/json; charset=utf-8", len(body))
        self.wfile.write(body)

    def _bytes(self, status: int, ctype: str, body: bytes, extra: Optional[dict] = None) -> None:
        self._headers(status, ctype, len(body), extra)
        self.wfile.write(body)

    def _error(self, status: int, code: str, message: str = "") -> None:
        self._json(status, {"error": code, "message": message or code})

    # ---- checks ---------------------------------------------------------------------------------------
    def _check_host(self) -> None:
        port = self.server_ref.port
        if self.headers.get("Host", "") not in (f"127.0.0.1:{port}", f"localhost:{port}"):
            raise _Reject(403, "BAD_HOST", "unexpected Host header")

    def _check_origin(self) -> None:
        port = self.server_ref.port
        if self.headers.get("Origin", "") not in (f"http://127.0.0.1:{port}", f"http://localhost:{port}"):
            raise _Reject(403, "BAD_ORIGIN", "missing or foreign Origin")

    def _cookie(self) -> Optional[str]:
        for part in self.headers.get("Cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE:
                return v
        return None

    def _check_console(self) -> None:
        sid = self._cookie()
        want = self.server_ref.csrf_for(sid) if sid else None
        got = self.headers.get("X-CSRF-Token", "")
        if not want or not secrets.compare_digest(want, got):
            raise _Reject(403, "CONSOLE_ONLY", "console session and CSRF token required")

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError:
            raise _Reject(400, "BAD_LENGTH") from None
        if n > MAX_BODY:
            raise _Reject(413, "TOO_LARGE", "request body too large")
        raw = self.rfile.read(n) if n else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise _Reject(400, "BAD_JSON", "body must be JSON") from None
        if not isinstance(body, dict):
            raise _Reject(400, "BAD_JSON", "body must be a JSON object")
        return body

    # ---- routing ----------------------------------------------------------------------------------------
    def do_GET(self):
        self._run("GET")

    def do_POST(self):
        self._run("POST")

    def _run(self, method: str) -> None:
        try:
            self._check_host()
            if method == "POST":
                self._refuse_large()
                self._check_origin()
            self._route(method, urlparse(self.path).path)
        except _Reject as e:
            self._error(e.status, e.code, e.message)
        except SessionError as e:
            self._error(e.status, e.code, e.message)
        except ValueError:
            self._error(400, "BAD_REQUEST")
        except Exception:  # noqa: BLE001 - never leak a traceback to the page
            self._error(500, "INTERNAL", "unexpected server error")

    def _refuse_large(self) -> None:
        try:
            n = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError:
            raise _Reject(400, "BAD_LENGTH") from None
        if n > MAX_BODY:
            self.close_connection = True
            left = min(n, 4 * 1024 * 1024)  # drain a bounded amount so the client can read the refusal
            while left > 0:
                chunk = self.rfile.read(min(left, 65536))
                if not chunk:
                    break
                left -= len(chunk)
            raise _Reject(413, "TOO_LARGE", "request body too large")

    def _page(self, name: str, csrf: Optional[str] = None, extra: Optional[dict] = None) -> None:
        html = (WEB / name).read_text(encoding="utf-8")
        if csrf is not None:
            html = html.replace("__CSRF__", csrf)
        self._bytes(200, "text/html; charset=utf-8", html.encode("utf-8"), extra)

    def _route(self, method: str, path: str) -> None:
        srv, s = self.server_ref, self.server_ref.session
        if method == "GET" and path == "/":
            sid = self._cookie()
            csrf = srv.csrf_for(sid) if sid else None
            if not csrf:  # a second tab of the same browser reuses its console; only a new browser gets a new one
                sid, csrf = srv.new_console()
            return self._page("console.html", csrf, {"Set-Cookie": f"{COOKIE}={sid}; HttpOnly; SameSite=Strict; Path=/"})
        if method == "GET" and path.startswith("/static/"):
            name = path[len("/static/"):]
            if name not in STATIC:
                raise _Reject(404, "NOT_FOUND")
            return self._bytes(200, STATIC[name], (WEB / name).read_bytes())
        m = re.fullmatch(r"/approve/([^/]+)", path)
        if method == "GET" and m:
            if not TOKEN_RX.match(m.group(1)):
                raise _Reject(404, "NOT_FOUND")
            return self._page("approver.html")
        m = re.fullmatch(r"/api/approver/([^/]+)(?:/([a-z]+))?", path)
        if m:
            tok, action = m.group(1), m.group(2)
            if not TOKEN_RX.match(tok):
                raise SessionError("UNKNOWN_TOKEN", "This approval link is not recognised.", 404)
            if method == "GET" and action is None:
                return self._json(200, s.approver_view(tok))
            if method == "POST" and action == "approve":
                b = self._body()
                return self._json(200, s.approver_approve(tok, str(b.get("digest", "")), str(b.get("typed", ""))))
            if method == "POST" and action == "deny":
                self._body()
                return self._json(200, s.approver_deny(tok))
            raise _Reject(404, "NOT_FOUND")
        if path.startswith("/api/"):
            self._check_console()
            if method == "GET" and path == "/api/state":
                return self._json(200, s.state())
            if method == "POST":
                return self._json(200, self._console_action(path[len("/api/"):], self._body()))
        raise _Reject(404, "NOT_FOUND")

    def _console_action(self, action: str, b: dict):
        s = self.server_ref.session
        eid = str(b.get("execution_id", ""))
        table = {
            "review/resolve": lambda: s.resolve_review(str(b.get("invoice_id", "")), str(b.get("field", "")),
                                                       str(b.get("value", "")), str(b.get("note", ""))),
            "lower-confidence": lambda: s.lower_confidence(str(b.get("receipt_id", ""))),
            "deposit": lambda: s.simulate_deposit(str(b.get("currency", "")), b.get("amount", "")),
            "prepare": lambda: s.prepare(str(b.get("obligation_id", "")), str(b.get("validity") or "MIN_1")),
            "execution": lambda: s.execution(eid),
            "dispatch": lambda: s.dispatch(eid),
            "advance": lambda: s.advance(eid),
            "reconcile": lambda: s.reconcile(eid),
            "requote": lambda: s.requote(eid),
            "transition": lambda: s.simulate_transition(eid, str(b.get("status", ""))),
            "record": lambda: s.record(eid),
            "verify": lambda: s.verify(b.get("bundle"), b.get("expected_digest") or None),
            "control/skip-clock": lambda: s.skip_clock(b.get("seconds")),
            "control/fake-rate": lambda: s.set_fake_rate(str(b.get("pair", "")), b.get("rate", "")),
            "control/withhold": lambda: s.withhold_next_response(str(b.get("op", ""))),
        }
        fn = table.get(action)
        if fn is None:
            raise _Reject(404, "NOT_FOUND")
        return fn()


def main(world: str = "fake", port: int = DEFAULT_PORT, stage: Optional[str] = None, reader: str = "scripted") -> int:
    model = None
    if reader == "model":
        from . import model_reader
        try:
            model = model_reader.make_reader()
        except model_reader.ReaderSetupError as e:
            print(f"The model reader cannot start: {e}", flush=True)
            return 2
        print(f"Invoice reader: {model.label}. Each invoice and each plan explanation is one model call.", flush=True)
    if world == "sandbox":
        from .sandbox import SandboxSetupError, build_sandbox_session
        try:
            session, pre = build_sandbox_session(commit=git_commit(), reader=model,
                                                event_log_path="evidence/events_sandbox.jsonl")
        except SandboxSetupError as e:
            print(f"The SANDBOX world cannot start: {e}", flush=True)
            return 2
        print(f"Sandbox preflight passed (read-only): world {pre['world']}, balances {pre['balances']}", flush=True)
        print("Starting the console creates the three fictional beneficiaries in the sandbox. Test money only.", flush=True)
    else:
        session, pre = build_fake_session(reader=model), None
    srv = ConsoleServer(session, port=port)
    q = "?stage=video" if stage == "video" else ""
    label = "WORLD: AIRWALLEX SANDBOX, test money" if pre and pre["world"] == "SANDBOX" else "WORLD: FAKE, local only"
    print(f"Rate-Locked console ({label}). Open {srv.base_url}/{q}", flush=True)
    what = ("the scripted TEST DOUBLE; no model is called" if model is None else f"a live model ({model.label})")
    print("Ctrl+C to stop." + (f" Nothing here calls Airwallex; the invoice reader is {what}." if pre is None else
                               f" The invoice reader is {what}."), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv._httpd.server_close()
    return 0

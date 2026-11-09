"""A replay server for tests: loopback HTTP that answers from canned, DOC-SHAPED responses and records every request.

It is a TEST DOUBLE of the Airwallex sandbox host, not a model of its behavior: it returns exactly what the test
registered for (method, path) and nothing else (404 otherwise). Responses are queued per route, so a test can
script "page 1, then page 2" or "an error, then a success". Requests (method, path, query, headers, parsed body)
are kept in `.requests` for assertions on what the adapter sent.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

FIXTURES = Path(__file__).parent / "fixtures" / "awx_docs"


def fixture(name: str) -> str:
    """Raw text of a DOC-SHAPED fixture (kept as text so numbers stay exact)."""
    return (FIXTURES / name).read_text(encoding="utf-8")


class Request:
    def __init__(self, method, path, query, headers, body_text):
        self.method, self.path, self.query, self.headers, self.body_text = method, path, query, headers, body_text

    @property
    def body(self):
        return json.loads(self.body_text) if self.body_text else None


class ReplayServer:
    def __init__(self):
        self.routes: dict = {}
        self.requests: list = []
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _go(self, method):
                u = urlparse(self.path)
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n).decode() if n else ""
                outer.requests.append(Request(method, u.path, {k: v[0] for k, v in parse_qs(u.query).items()},
                                              dict(self.headers), body))
                queue = outer.routes.get((method, u.path))
                if not queue:
                    status, text = 404, '{"code":"not_found","message":"no canned response"}'
                else:
                    status, text = queue[0] if len(queue) == 1 else queue.pop(0)
                raw = text.encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                self._go("GET")

            def do_POST(self):
                self._go("POST")

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self._t = threading.Thread(target=lambda: self._httpd.serve_forever(poll_interval=0.02), daemon=True)

    def on(self, method, path, text, status=200):
        """Queue one canned response. The last one registered for a route repeats forever."""
        self.routes.setdefault((method, "/api/v1" + path), []).append((status, text))
        return self

    def sent(self, method, path):
        return [r for r in self.requests if r.method == method and r.path == "/api/v1" + path]

    @property
    def base_url(self):
        h, p = self._httpd.server_address[:2]
        return f"http://{h}:{p}"

    def __enter__(self):
        self.on("POST", "/authentication/login", fixture("login.json"), 201)
        self._t.start()
        return self

    def __exit__(self, *a):
        self._httpd.shutdown()
        self._httpd.server_close()

"""HTTP face of the local fake: Airwallex-shaped endpoints on loopback, no keys needed.

Run `python -m rate_locked.awx.fake_server` and point an adapter (or the sandbox probe,
`--base-url http://127.0.0.1:PORT`) at it. Shapes come from wire.py (ASSUMED until the live probe).

Conventions the fake enforces so adapter bugs show up early:
  * every call except login needs `Authorization: Bearer <token>`; tokens last 30 fake-minutes (401 after);
  * FX calls that carry an `x-api-version` header are rejected (builder guide: leave it off FX calls);
  * a lost response or lost request (FakeAirwallex.inject) closes the connection without answering.
"""
from __future__ import annotations

import secrets
import socket
import threading
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import parse_qs, urlparse
from decimal import Decimal

from . import wire
from .adapter import AirwallexError, UncertainOutcome
from .fake import FakeAirwallex

TOKEN_TTL = timedelta(minutes=30)


class FakeServer:
    def __init__(self, fake: FakeAirwallex, host: str = "127.0.0.1", port: int = 0,
                 credentials: Optional[tuple] = None):
        self.fake = fake
        self.credentials = credentials
        self.tokens: dict = {}
        self.requests: list = []  # (method, path) log for tests
        self._lock = threading.Lock()
        outer = self

        class Handler(_Handler):
            server_ref = outer

        self._httpd = ThreadingHTTPServer((host, port), Handler)
        self._thread = threading.Thread(target=lambda: self._httpd.serve_forever(poll_interval=0.02), daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> "FakeServer":
        self._thread.start()
        return self

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.close()


class _Handler(BaseHTTPRequestHandler):
    server_ref: FakeServer
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # quiet
        pass

    # ---- plumbing ------------------------------------------------------------------------------
    def _send(self, status: int, payload) -> None:
        raw = wire.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _drop(self) -> None:
        self.close_connection = True
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        return wire.loads(self.rfile.read(n).decode())

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        srv = self.server_ref
        url = urlparse(self.path)
        path = url.path.rstrip("/")
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        with srv._lock:
            srv.requests.append((method, path))
            try:
                body = self._body() if method == "POST" else {}
                status, payload = self._route(method, path, query, body)
            except UncertainOutcome:
                self._drop()
                return
            except AirwallexError as e:
                status, payload = e.http_status, wire.error_to_wire(e.code, e.message)
            except (KeyError, ValueError) as e:
                status, payload = 400, wire.error_to_wire("validation_error", f"bad request: {e}")
        self._send(status, payload)

    # ---- auth ---------------------------------------------------------------------------------------
    def _check_auth(self) -> None:
        srv = self.server_ref
        header = self.headers.get("Authorization", "")
        token = header[7:] if header.startswith("Bearer ") else ""
        expires = srv.tokens.get(token)
        if not expires or srv.fake.clock.now() >= expires:
            raise AirwallexError("unauthorized", "missing, unknown or expired token", 401)

    # ---- routes ---------------------------------------------------------------------------------------
    def _route(self, method, path, query, body):
        srv, f = self.server_ref, self.server_ref.fake
        p = path.replace("/api/v1", "", 1) if path.startswith("/api/v1") else path
        if method == "POST" and p == "/authentication/login":
            cid, key = self.headers.get("x-client-id", ""), self.headers.get("x-api-key", "")
            if not cid or not key or (srv.credentials and (cid, key) != srv.credentials):
                raise AirwallexError("credentials_invalid", "bad client id or api key", 401)
            token = secrets.token_urlsafe(24)
            expires = f.clock.now() + TOKEN_TTL
            srv.tokens[token] = expires
            return 201, {"token": token, "expires_at": wire.ts(expires)}
        self._check_auth()
        if p.startswith("/fx/") and self.headers.get("x-api-version"):
            raise AirwallexError("unexpected_api_version", "leave x-api-version off FX calls (builder guide)")

        if method == "GET" and p == "/balances/current":
            return 200, wire.balances_to_wire(f.get_balances())
        if method == "GET" and p == "/fx/rates/current":
            r = f.get_current_rate(
                sell_currency=query["sell_currency"], buy_currency=query["buy_currency"],
                buy_amount=Decimal(query["buy_amount"]) if "buy_amount" in query else None,
                sell_amount=Decimal(query["sell_amount"]) if "sell_amount" in query else None)
            return 200, wire.rate_to_wire(r)
        if method == "POST" and p == "/fx/quotes/create":
            q = f.create_quote(
                buy_currency=body["buy_currency"], sell_currency=body["sell_currency"], validity=body["validity"],
                buy_amount=_d(body.get("buy_amount")), sell_amount=_d(body.get("sell_amount")))
            return 201, wire.quote_to_wire(q)
        if method == "GET" and p.startswith("/fx/quotes/"):
            return 200, wire.quote_to_wire(f.get_quote(p.rsplit("/", 1)[1]))
        if method == "POST" and p == "/fx/conversions/create":
            c = f.create_conversion(
                request_id=body["request_id"], quote_id=body.get("quote_id"), buy_currency=body["buy_currency"],
                sell_currency=body["sell_currency"], buy_amount=_d(body.get("buy_amount")),
                sell_amount=_d(body.get("sell_amount")), reason=body.get("reason", ""))
            return 201, wire.conversion_to_wire(c)
        if method == "GET" and p == "/fx/conversions":
            items = f.find_conversions(query.get("request_id", ""))
            return 200, {"items": [wire.conversion_to_wire(c) for c in items], "has_more": False}
        if method == "GET" and p.startswith("/fx/conversions/"):
            return 200, wire.conversion_to_wire(f.get_conversion(p.rsplit("/", 1)[1]))
        if method == "GET" and p == "/global_accounts":
            return 200, {"items": [{"id": f"ga-fake-{b.currency}", "account_name": "Demo Trading Co. (fake)",
                                    "country_code": "GB", "status": "ACTIVE",
                                    "supported_features": [{"currency": b.currency, "type": "DEPOSIT",
                                                            "transfer_method": "LOCAL"}]}
                                   for b in f.get_balances()], "page_after": None, "page_before": None}
        if method == "POST" and p in ("/beneficiaries/schema", "/beneficiary_api_schemas/generate"):
            return 200, f.get_beneficiary_schema(
                country_code=body.get("country_code", ""), currency=body.get("currency", ""),
                entity_type=body.get("entity_type", ""), transfer_method=body.get("transfer_method", ""))
        if method == "POST" and p == "/beneficiaries/create":
            b = f.create_beneficiary(request_id=body["request_id"], payload=body.get("beneficiary", {}))
            return 201, wire.beneficiary_to_wire(b)
        if method == "POST" and p == "/transfers/validate":
            f.validate_transfer(beneficiary_id=body["beneficiary_id"], source_currency=body["source_currency"],
                                transfer_amount=_d(body["transfer_amount"]))
            return 200, {"valid": True}
        if method == "POST" and p == "/transfers/create":
            t = f.create_transfer(
                request_id=body["request_id"], beneficiary_id=body["beneficiary_id"],
                source_currency=body["source_currency"], transfer_currency=body["transfer_currency"],
                transfer_amount=_d(body["transfer_amount"]), reason=body.get("reason", ""),
                reference=body.get("reference", ""), transfer_method=body.get("transfer_method", "LOCAL"))
            return 201, wire.transfer_to_wire(t)
        if method == "GET" and p == "/transfers":
            items = f.find_transfers(query.get("request_id", ""), beneficiary_id=query.get("beneficiary_id"))
            return 200, {"items": [wire.transfer_to_wire(t) for t in items], "page_after": None, "page_before": None}
        if method == "GET" and p.startswith("/transfers/"):
            return 200, wire.transfer_to_wire(f.get_transfer(p.rsplit("/", 1)[1]))
        if method == "POST" and p == "/simulation/deposit/create":
            gid = str(body["global_account_id"])
            if not gid.startswith("ga-fake-"):
                raise AirwallexError("not_found", "unknown global account", 404)
            d = f.simulate_deposit(request_id="dep-" + secrets.token_hex(8), currency=gid[len("ga-fake-"):],
                                   amount=_d(body["amount"]))
            return 201, wire.deposit_to_wire(d)
        if method == "POST" and p.startswith("/simulation/transfers/") and p.endswith("/transition"):
            tid = p.split("/")[3]
            return 200, wire.transfer_to_wire(f.simulate_transfer_transition(tid, body["next_status"]))
        raise AirwallexError("not_found", f"{method} {path}", 404)


def _d(v):
    return None if v is None else Decimal(str(v))


def main() -> None:  # pragma: no cover - manual use
    import argparse
    ap = argparse.ArgumentParser(description="Run the local Airwallex fake on loopback.")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    srv = FakeServer(FakeAirwallex.demo(), port=args.port).start()
    print(f"Local fake listening at {srv.base_url} (any non-empty client id and key). Ctrl+C to stop.")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        srv.close()


if __name__ == "__main__":  # pragma: no cover
    main()

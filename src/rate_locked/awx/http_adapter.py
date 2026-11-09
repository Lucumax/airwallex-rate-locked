"""HTTP adapter for the Airwallex sandbox, written against the documented shapes and the local fake server.

It ran against the live sandbox on 2026-11-05 (evidence/probe_*.json). Response shapes are confirmed only for the
fields those recordings contain (see wire.py and `python -m rate_locked check-shapes`); everything else is as documented.

Contract (tests/adapter_contract.py must pass against this class):
  * host lock: https://api.sandbox.airwallex.com, or loopback for the local fake;
  * never retries a create call; connection loss or timeout after sending -> UncertainOutcome;
  * mints nothing: request_ids come from the caller;
  * never sends x-api-version;
  * never logs or prints the API key or bearer token.
"""
from __future__ import annotations

import http.client
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

from . import wire
from .adapter import AirwallexError, UncertainOutcome

SANDBOX_BASE = "https://api.sandbox.airwallex.com"
MAX_PAGES = 5
MAX_ERROR_TEXT = 200
_CODE = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_LONG_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{7,}")
_LOOPBACK = ("127.0.0.1", "localhost", "::1")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):
        return None


def check_host(base_url: str) -> str:
    """A bare origin only: the sandbox host, or loopback http for the local fake."""
    base = base_url.rstrip("/")
    if "\\" in base or any(ch.isspace() for ch in base):
        raise ValueError("host lock: unexpected characters in the base URL")
    u = urllib.parse.urlparse(base)
    if u.username or u.password or u.query or u.fragment or u.params:
        raise ValueError("host lock: no credentials, query or fragment allowed in the base URL")
    if base == SANDBOX_BASE:
        return SANDBOX_BASE
    if u.scheme == "http" and u.hostname in _LOOPBACK and u.path in ("", "/"):
        return base
    raise ValueError("host lock: only the Airwallex sandbox or a bare loopback origin is allowed")


class HttpAdapter:
    def __init__(self, base_url: str, client_id: str, api_key: str, *, timeout: float = 20.0, clock=None):
        self.base = check_host(base_url)
        self._client_id, self._api_key = client_id, api_key
        self.timeout = timeout
        self._clock = clock  # only for token expiry bookkeeping; defaults to wall time
        self._token: Optional[str] = None
        self._token_until: Optional[datetime] = None
        self._ben_for: dict = {}  # transfer id -> beneficiary id the create established (a read may omit it)
        self._opener = urllib.request.build_opener(_NoRedirect)

    def __repr__(self):  # never leak credentials
        return f"HttpAdapter(base={self.base!r})"

    def _safe_text(self, text) -> str:
        """Provider text for an exception: our credentials removed, any long identifier-like token (account numbers,
        IBANs, ids) replaced, and bounded. Provider messages can echo what we sent, so none is kept verbatim."""
        text = str(text)
        for s in sorted((x for x in (self._client_id, self._api_key, self._token) if x and len(x) >= 4), key=len,
                        reverse=True):
            text = text.replace(s, "[redacted]")
        text = _LONG_TOKEN.sub(lambda m: m.group(0) if not any(c.isdigit() for c in m.group(0)) else "[redacted]", text)
        return text if len(text) <= MAX_ERROR_TEXT else text[:MAX_ERROR_TEXT] + "...[truncated]"

    # ---- transport -------------------------------------------------------------------------------
    def _now(self) -> datetime:
        return self._clock.now() if self._clock else datetime.now(timezone.utc)

    def _login(self) -> None:
        req = urllib.request.Request(self.base + "/api/v1/authentication/login", data=b"", method="POST",
                                     headers={"x-client-id": self._client_id, "x-api-key": self._api_key})
        status, data = self._send(req, op="login", request_id="")
        if status >= 400:
            raise AirwallexError(self._code(data, "login_failed"), self._safe_text(data.get("message", "")), status)
        self._token = data["token"]
        self._token_until = wire.parse_ts(data["expires_at"]) - timedelta(seconds=60)

    def _ensure_token(self) -> None:
        if not self._token or self._now() >= self._token_until:
            self._login()

    def _send(self, req: urllib.request.Request, *, op: str, request_id: str, create: bool = False):
        """A read or login that loses its connection is tried once more (the live sandbox drops a first call at
        times). Nothing that creates is ever sent twice."""
        try:
            return self._send_once(req, op=op, request_id=request_id, create=create)
        except AirwallexError as e:
            if e.code != "transport_error" or create or not (req.get_method() == "GET" or op == "login"):
                raise
            return self._send_once(req, op=op, request_id=request_id, create=create)

    def _send_once(self, req: urllib.request.Request, *, op: str, request_id: str, create: bool = False):
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                status, raw = resp.status, resp.read()
        except urllib.error.HTTPError as e:
            status = e.code
            try:
                raw = e.read()
            except Exception:  # noqa: BLE001 - a partial error body is still a definite HTTP answer
                raw = b""
            if create and status >= 500:
                raise UncertainOutcome(op, request_id, f"HTTP {status}") from None
        except (http.client.HTTPException, socket.timeout, TimeoutError, ConnectionError, OSError) as e:
            if create:
                raise UncertainOutcome(op, request_id, type(e).__name__) from None
            raise AirwallexError("transport_error", type(e).__name__, 0) from None
        try:
            data = wire.loads(raw.decode("utf-8") or "{}")
        except (UnicodeDecodeError, ValueError):
            if create:  # an unreadable answer to a create cannot establish failure: the request may be live
                raise UncertainOutcome(op, request_id, "unusable response body") from None
            if status >= 400 and status < 500:
                return status, {}  # a read refused with an unreadable body is an ordinary error
            raise AirwallexError("malformed_response", "response was not valid JSON", status) from None
        return status, data

    def _parse(self, fn, payload, *, op="", request_id="", create=False):
        """Turn a wire object into an adapter type. A create whose answer cannot be read is uncertain."""
        try:
            return fn(payload)
        except (KeyError, TypeError, ValueError, ArithmeticError, AttributeError):
            if create:
                raise UncertainOutcome(op, request_id, "response missing expected fields") from None
            raise AirwallexError("malformed_response", "response missing expected fields", 0) from None

    def warm_up(self) -> None:
        """Make sure a valid token exists so the next call does not log in first (used right before a send)."""
        self._ensure_token()

    def _call(self, method, path, *, body=None, query=None, op="", request_id="", create=False):
        self._ensure_token()
        url = self.base + "/api/v1" + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = wire.dumps(body).encode() if body is not None else None
        headers = {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"}
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        status, payload = self._send(req, op=op or path, request_id=request_id, create=create)
        if status == 401 and not create:  # one token refresh for reads only; a create is never re-sent
            self._token = None
            self._ensure_token()
            req = urllib.request.Request(url, data=data, method=method,
                                         headers={**headers, "Authorization": f"Bearer {self._token}"})
            status, payload = self._send(req, op=op or path, request_id=request_id, create=create)
        if status >= 400:
            if create and not (isinstance(payload, dict) and _CODE.fullmatch(str(payload.get("code") or ""))):
                # only a decoded, classifiable rejection proves a create failed; anything else may be a live request
                raise UncertainOutcome(op or path, request_id, f"HTTP {status} without a recognised error code")
            if not isinstance(payload, dict):
                payload = {}
            raise AirwallexError(self._code(payload, "error"), self._safe_text(payload.get("message", "")), status)
        return payload

    @staticmethod
    def _code(payload, default: str) -> str:
        code = str(payload.get("code") or "")
        return code if _CODE.fullmatch(code) else default

    def _create_call(self, path, body, *, op, request_id):
        """A create call. A provider answer that says the request_id is already known (400 request_id_duplicate) or
        still in flight (409 request_pending) is reported verbatim; the executor turns it into a lookup. Never re-sent."""
        return self._call("POST", path, body=body, op=op, request_id=request_id, create=True)

    def _pages(self, path, query, *, op, mode):
        """All items of a list endpoint, bounded. `mode` is 'num' (page_num + has_more, conversions) or 'bookmark'
        (page=page_after, transfers and global accounts). Still more after MAX_PAGES: fail closed, never a partial list."""
        items, q = [], dict(query)
        for n in range(MAX_PAGES):
            if mode == "num":
                q["page_num"] = str(n)
            data = self._call("GET", path, query=q, op=op)
            if not isinstance(data, dict) or not isinstance(data.get("items"), list):
                raise AirwallexError("malformed_response", "list response has no items array", 0)
            items.extend(data["items"])
            if mode == "num":
                more = bool(data.get("has_more"))
            else:
                nxt = data.get("page_after")
                more = bool(nxt) or bool(data.get("has_more"))
                if more and not nxt:
                    raise AirwallexError("incomplete_lookup", "has_more without a page bookmark", 0)
                q["page"] = nxt
            if not more:
                return items
        raise AirwallexError("incomplete_lookup", f"result spans more than {MAX_PAGES} pages", 0)

    # ---- AirwallexAdapter ---------------------------------------------------------------------------
    def get_balances(self):
        return self._parse(wire.balances_from_wire, self._call("GET", "/balances/current"))

    def get_current_rate(self, *, sell_currency, buy_currency, buy_amount=None, sell_amount=None):
        q = {"sell_currency": sell_currency, "buy_currency": buy_currency}
        if buy_amount is not None:
            q["buy_amount"] = str(buy_amount)
        if sell_amount is not None:
            q["sell_amount"] = str(sell_amount)
        return self._parse(wire.rate_from_wire, self._call("GET", "/fx/rates/current", query=q))

    def create_quote(self, *, buy_currency, sell_currency, validity, buy_amount=None, sell_amount=None):
        body = {"buy_currency": buy_currency, "sell_currency": sell_currency, "validity": validity}
        if buy_amount is not None:
            body["buy_amount"] = buy_amount
        if sell_amount is not None:
            body["sell_amount"] = sell_amount
        return self._parse(wire.quote_from_wire, self._call("POST", "/fx/quotes/create", body=body, op="create_quote"))

    def get_quote(self, quote_id):
        return self._parse(wire.quote_from_wire, self._call("GET", f"/fx/quotes/{quote_id}"))

    def create_conversion(self, *, request_id, quote_id, buy_currency, sell_currency,
                          buy_amount=None, sell_amount=None, reason=""):
        body = wire.conversion_request_body(request_id=request_id, quote_id=quote_id, buy_currency=buy_currency,
                                            sell_currency=sell_currency, buy_amount=buy_amount,
                                            sell_amount=sell_amount, reason=reason)
        data = self._create_call("/fx/conversions/create", body, op="create_conversion", request_id=request_id)
        return self._parse(wire.conversion_from_wire, data, op="create_conversion", request_id=request_id, create=True)

    def get_conversion(self, conversion_id):
        return self._parse(wire.conversion_from_wire, self._call("GET", f"/fx/conversions/{conversion_id}"))

    def find_conversions(self, request_id):
        items = self._pages("/fx/conversions", {"request_id": request_id}, op="find_conversions", mode="num")
        return self._parse(lambda xs: [wire.conversion_from_wire(x) for x in xs], items)

    def get_beneficiary_schema(self, *, country_code, currency, entity_type, transfer_method):
        body = {"country_code": country_code, "currency": currency, "entity_type": entity_type,
                "transfer_method": transfer_method}
        return self._call("POST", "/beneficiaries/schema", body=body)

    def create_beneficiary(self, *, request_id, payload):
        data = self._create_call("/beneficiaries/create",
                                 wire.beneficiary_create_body(request_id=request_id, beneficiary=payload),
                                 op="create_beneficiary", request_id=request_id)
        return self._parse(lambda d: wire.beneficiary_from_wire(d, request_id), data, op="create_beneficiary",
                           request_id=request_id, create=True)

    def create_transfer(self, *, request_id, beneficiary_id, source_currency, transfer_currency,
                        transfer_amount, reason, reference, transfer_method="LOCAL"):
        body = wire.transfer_request_body(request_id=request_id, beneficiary_id=beneficiary_id,
                                          source_currency=source_currency, transfer_currency=transfer_currency,
                                          transfer_amount=transfer_amount, reason=reason, reference=reference,
                                          transfer_method=transfer_method)
        data = self._create_call("/transfers/create", body, op="create_transfer", request_id=request_id)
        t = self._parse(lambda d: wire.transfer_from_wire(d, beneficiary_id), data, op="create_transfer",
                        request_id=request_id, create=True)
        self._ben_for[t.transfer_id] = t.beneficiary_id
        return t

    def get_transfer(self, transfer_id):
        return self._parse(lambda d: wire.transfer_from_wire(d, self._ben_for.get(transfer_id)),
                           self._call("GET", f"/transfers/{transfer_id}"))

    def find_transfers(self, request_id, *, beneficiary_id=None):
        q = {"request_id": request_id}
        if beneficiary_id:
            q["beneficiary_id"] = beneficiary_id  # documented filter; also what lets us attach the id to the records
        items = self._pages("/transfers", q, op="find_transfers", mode="bookmark")
        found = self._parse(lambda xs: [wire.transfer_from_wire(x, beneficiary_id) for x in xs], items)
        for t in found:
            self._ben_for.setdefault(t.transfer_id, t.beneficiary_id)
        return found

    def simulate_deposit(self, *, request_id, currency, amount):
        """Sandbox only. The reference wants a global_account_id, so the first ACTIVE account that accepts deposits
        in `currency` is looked up (a read). Nothing is sent when there is none."""
        accounts = self._pages("/global_accounts", {"status": "ACTIVE"}, op="list_global_accounts", mode="bookmark")
        gid = self._parse(lambda xs: wire.global_account_for(xs, currency), accounts)
        if gid is None:
            raise AirwallexError("no_global_account", f"no active global account accepts {currency} deposits", 0)
        data = self._create_call("/simulation/deposit/create",
                                 wire.deposit_request_body(global_account_id=gid, amount=amount),
                                 op="simulate_deposit", request_id=request_id)
        return self._parse(lambda d: wire.deposit_from_wire(d, request_id), data, op="simulate_deposit",
                           request_id=request_id, create=True)

    def simulate_transfer_transition(self, transfer_id, transition):
        data = self._call("POST", f"/simulation/transfers/{transfer_id}/transition", body=wire.transition_body(transition))
        return self._parse(lambda d: wire.transfer_from_wire(d, self._ben_for.get(transfer_id)), data)

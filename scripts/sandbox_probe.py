#!/usr/bin/env python3
"""Sandbox probe: one command, sanitized output, Airwallex sandbox only.

    python scripts/sandbox_probe.py                 # read-only plus quotes (no money moves)
    python scripts/sandbox_probe.py --convert       # also P7 and P9: one smallest sandbox conversion at the quote
    python scripts/sandbox_probe.py --expiry-test   # also P8: waits 65 s, then tries an expired quote
    python scripts/sandbox_probe.py --simulate-deposit --docs-mcp
    python scripts/sandbox_probe.py --dry-run       # print the plan, call nothing, needs no keys

Reads AIRWALLEX_CLIENT_ID and AIRWALLEX_API_KEY from the environment of the shell that runs it.
Never prints, stores or sends them anywhere except the login call to the sandbox host.
Standard library only. Writes evidence/probe_<UTC time>.json (sanitized, saved after every step and BEFORE
every financial call) and prints a short table.
Do not paste the keys anywhere. Paste only the printed table, or the JSON file after reading it once.

Host lock: https://api.sandbox.airwallex.com only (loopback http is allowed for the local fake, and the public
docs MCP host for the optional --docs-mcp step). Every request is checked against that list, the bearer token
is only ever attached to the sandbox or loopback origin, and redirects are refused. No x-api-version header is
ever sent (the builder guide says to leave it off FX calls). Every create call gets a fresh UUID request_id
recorded before the call, and nothing is retried. If a conversion's outcome is uncertain the probe looks it up
by its request_id and says so.

Step ids follow the spec (P1 to P10). Every response shape here is a discovery: the point is to record
what the sandbox really answers so wire.py can be corrected.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

VERSION = "2"
SANDBOX = "https://api.sandbox.airwallex.com"
DOCS_MCP = "https://mcp.sandbox.airwallex.com/docs"
LOOPBACK = ("127.0.0.1", "localhost", "::1")
SCHEMA_PATHS = ("/api/v1/beneficiaries/schema", "/api/v1/beneficiary_api_schemas/generate")  # guide path, reference path (neither confirmed by the reference pages read on 2026-11-01)
EXPIRED_CODES = ("expired_quote", "invalid_quote_id")

SECRET_KEY = re.compile(r"(token|secret|api[_-]?key|authorization|password|passwd|credential)", re.I)
PRIVATE_KEY = re.compile(r"(account_number|iban|routing|swift|bic|sort_code|bsb|card_number|email|phone|address|"
                         r"tax_|date_of_birth|dob|national_id)", re.I)


class HostError(ValueError):
    pass


def check_base(base: str) -> str:
    """A bare origin only: the sandbox host, or loopback http for the local fake. No userinfo, path, query or fragment."""
    base = base.rstrip("/")
    u = urllib.parse.urlparse(base)
    if u.username or u.password or u.query or u.fragment or u.params:
        raise HostError(f"refusing {base!r}: no credentials, query or fragment allowed in the base URL")
    if base == SANDBOX:
        return base
    if "\\" in base or any(ch.isspace() for ch in base):
        raise HostError(f"refusing {base!r}: unexpected characters in the base URL")
    if u.scheme == "http" and u.hostname in LOOPBACK and u.path in ("", "/"):
        return base
    raise HostError(f"refusing {base!r}: only {SANDBOX} (or a bare loopback origin for the local fake) is allowed")


def _origin_ok(url: str, allow_docs: bool = False) -> bool:
    u = urllib.parse.urlparse(url)
    if u.username or u.password:
        return False
    if u.scheme == "https" and u.hostname == "api.sandbox.airwallex.com" and u.port in (None, 443):
        return True
    if u.scheme == "http" and u.hostname in LOOPBACK:
        return True
    return allow_docs and url == DOCS_MCP


def _auth_origin_ok(url: str) -> bool:
    return _origin_ok(url, allow_docs=False)


# ---- sanitizing ---------------------------------------------------------------------------------

def private_values(entry) -> tuple:
    """String leaves of a private beneficiary entry that must never appear in evidence or output, even inside a
    provider's free-text error message: everything under bank_details (6+ characters) and any value under a key that
    looks private (4+ characters)."""
    out = []

    def walk(o, under_bank=False, private_key=False):
        if isinstance(o, dict):
            for k, v in o.items():
                walk(v, under_bank or str(k) == "bank_details", private_key or bool(PRIVATE_KEY.search(str(k))))
        elif isinstance(o, (list, tuple)):
            for v in o:
                walk(v, under_bank, private_key)
        elif isinstance(o, (str, int)) and not isinstance(o, bool):
            s = str(o)
            if (under_bank and len(s) >= 6) or (private_key and len(s) >= 4):
                out.append(s)
    walk(entry)
    return tuple(dict.fromkeys(out))


LOOKUP_CODES = ("request_id_duplicate", "request_pending", "duplicate_request")


def create_outcome_uncertain(step, data) -> bool:
    """True when a create call has no answer that proves it failed or succeeded: transport error, 5xx, 409, a duplicate
    or pending code, an unreadable or code-less rejection, or a 2xx whose body is not a usable object."""
    status = step["http_status"]
    code = data.get("code") if isinstance(data, dict) else None
    if status in (0, None) or status >= 500 or status == 409 or code in LOOKUP_CODES:
        return True
    if status >= 400:
        return not (isinstance(code, str) and code)
    return bool(step["ok"])


def mask(value: str) -> str:
    value = str(value)
    return "****" + value[-4:] if len(value) > 4 else "****"


def scrub(text, secrets=()) -> str:
    text = str(text)
    for s in sorted((s for s in secrets if s and len(s) >= 4), key=len, reverse=True):
        text = text.replace(s, "[redacted]")
    return text


def sanitize(obj, secrets=(), _depth=0):
    """Redact secrets (in keys and values) and private fields, mask account-like values, truncate.
    Pure; never raises on odd input. Secrets are removed BEFORE any partial masking."""
    secrets = tuple(s for s in secrets if s)
    if _depth > 8:
        return "[too deep]"
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            key = scrub(k, secrets)
            if SECRET_KEY.search(str(k)):
                out[key] = "[redacted]"
            elif PRIVATE_KEY.search(str(k)) and isinstance(v, (str, int)):
                out[key] = "[redacted]" if scrub(v, secrets) != str(v) else mask(v)
            else:
                out[key] = sanitize(v, secrets, _depth + 1)
        return out
    if isinstance(obj, (list, tuple)):
        items = [sanitize(v, secrets, _depth + 1) for v in obj[:20]]
        if len(obj) > 20:
            items.append(f"[{len(obj) - 20} more items not shown]")
        return items
    if isinstance(obj, str):
        obj = scrub(obj, secrets)
        return obj if len(obj) <= 300 else obj[:300] + "...[truncated]"
    if isinstance(obj, Decimal):
        return str(obj)
    return obj


def encode(obj) -> str:
    """JSON with Decimals written as exact numbers (never through binary floats)."""
    if isinstance(obj, Decimal):
        if not obj.is_finite():
            raise ValueError("non-finite Decimal cannot be serialized")
        return format(obj, "f")
    if isinstance(obj, (bool, int, str)) or obj is None:
        return json.dumps(obj)
    if isinstance(obj, float):
        raise TypeError("use Decimal, not float")
    if isinstance(obj, dict):
        return "{" + ",".join(json.dumps(str(k)) + ":" + encode(v) for k, v in obj.items()) + "}"
    if isinstance(obj, (list, tuple)):
        return "[" + ",".join(encode(v) for v in obj) + "]"
    raise TypeError(type(obj))


# ---- transport ----------------------------------------------------------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def http(method, url, *, headers=None, body=None, timeout=20):
    """Returns (status, parsed_json_or_raw, response_headers). Transport failures return status 0."""
    headers = headers or {}
    if not _origin_ok(url):
        raise HostError(f"refusing to call {url!r}")
    if any(k.lower() == "authorization" for k in headers) and not _auth_origin_ok(url):
        raise HostError("refusing to attach a bearer token to this origin")
    data = None if body is None else encode(body).encode()
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with _opener.open(req, timeout=timeout) as resp:
            raw, status, hdrs = resp.read().decode("utf-8", "replace"), resp.status, dict(resp.headers)
    except urllib.error.HTTPError as e:
        try:
            raw = e.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 - an unreadable error body is still a definite HTTP status
            raw = ""
        status, hdrs = e.code, dict(e.headers or {})
    except Exception as e:  # noqa: BLE001 - recorded, not hidden
        return 0, {"_transport_error": type(e).__name__}, {}
    try:
        return status, json.loads(raw, parse_float=Decimal, parse_constant=_no_constants) if raw else {}, hdrs
    except ValueError:
        # never keep the raw text: it could echo a secret we cannot recognize
        return status, {"_non_json": True, "length": len(raw)}, hdrs


def _no_constants(name):
    raise ValueError(f"non-finite number {name}")


def parse_sse_or_json(text: str):
    """Docs MCP may answer JSON or an event stream; return the first JSON-RPC message found."""
    text = text.strip()
    if text.startswith("{"):
        return json.loads(text)
    for line in text.splitlines():
        if line.startswith("data:"):
            payload = line[5:].strip()
            if payload.startswith("{"):
                return json.loads(payload)
    raise ValueError("no JSON-RPC message found")


# ---- the probe ------------------------------------------------------------------------------------

class Probe:
    def __init__(self, base, client_id, api_key, *, sleep=time.sleep, evidence_path=None, meta=None, private=()):
        self.base = check_base(base)
        self._private = tuple(private)  # the owner's private beneficiary values: scrubbed wherever they appear
        self._cid, self._key = client_id, api_key
        self._token = None
        self.sleep = sleep
        self.steps: list = []
        self.intents: list = []
        self.facts: dict = {}
        self.hint = ""
        self.evidence_path = evidence_path
        self.meta = meta or {}

    def _secrets(self):
        return (self._cid, self._key, self._token, *self._private)

    # evidence is rebuilt, sanitized as ONE object, and saved after every step
    def evidence(self) -> dict:
        sec = self._secrets()
        raw = {**self.meta, "facts": self.facts, "decision_hint": self.hint, "intents": self.intents}
        out = sanitize(raw, sec)
        out["steps"] = [sanitize(s, sec) for s in self.steps]  # every step is kept; only long response lists are cut
        return out

    def persist(self) -> None:
        if self.evidence_path:
            Path(self.evidence_path).parent.mkdir(parents=True, exist_ok=True)
            Path(self.evidence_path).write_text(json.dumps(self.evidence(), indent=2, sort_keys=True),
                                                encoding="utf-8", newline="\n")

    def call(self, sid, name, method, path, *, body=None, query=None, auth=True, accept=(200, 299), expect_codes=None,
             extra_headers=None, note="", financial=None):
        url = self.base + path + ("?" + urllib.parse.urlencode(query) if query else "")
        headers = {"Content-Type": "application/json", **(extra_headers or {})}
        if auth:
            headers["Authorization"] = f"Bearer {self._token}"
        if financial:  # intent first, on disk, before anything is sent
            self.intents.append({"op": financial, "request_id": (body or {}).get("request_id"),
                                 "at": datetime.now(timezone.utc).isoformat(), "step": sid})
            self.persist()
        t0 = time.monotonic()
        status, data, hdrs = http(method, url, headers=headers, body=body)
        ok = accept[0] <= status <= accept[1]
        if expect_codes is not None:
            ok = status >= 400 and isinstance(data, dict) and data.get("code") in expect_codes
        step = {
            "id": sid, "name": name, "method": method, "path": path + ("?" + urllib.parse.urlencode(query) if query else ""),
            "request": body, "http_status": status, "ok": ok,
            "elapsed_ms": int((time.monotonic() - t0) * 1000), "response": data,
            "version_headers": {k: v for k, v in hdrs.items() if "version" in k.lower()}, "note": note,
        }
        self.steps.append(step)
        self.persist()
        return step, data

    # P1
    def login(self):
        step, data = self.call("P1", "authenticate", "POST", "/api/v1/authentication/login", auth=False,
                               extra_headers={"x-client-id": self._cid, "x-api-key": self._key}, body=None)
        if step["ok"] and isinstance(data, dict) and data.get("token"):
            self._token = data["token"]
            step["response"] = {"token": "[redacted]", "expires_at": data.get("expires_at")}
            self.persist()
            return True
        step["ok"] = False
        step["note"] = "No token. Check the key's scope, that it is a sandbox key, and the client id."
        self.persist()
        return False

    def _lookup_conversion(self, rid):
        for wait in (0, 2, 3, 15):  # cumulative about 0, 2, 5, 20 s
            self.sleep(wait)
            s9, lst = self.call("P9", f"lookup by request_id after about {wait}s extra", "GET", "/api/v1/fx/conversions",
                                query={"request_id": rid})
            items = lst.get("items") if isinstance(lst, dict) else None
            s9["note"] = f"items found: {len(items) if isinstance(items, list) else 'unknown shape'}"

    def run(self, opts):
        if not self.login():
            return
        s, data = self.call("P2", "balances", "GET", "/api/v1/balances/current")
        if s["ok"] and isinstance(data, list):
            try:
                zero = sorted(str(b.get("currency", "?")) for b in data if Decimal(str(b.get("available_amount", 0))) == 0)
            except Exception:  # noqa: BLE001 - unknown shape
                zero = []
            self.facts["currencies"] = sorted(str(b.get("currency", "?")) for b in data)
            self.facts["balances"] = {str(b.get("currency")): str(b.get("available_amount")) for b in data
                                      if isinstance(b, dict) and b.get("account_type", "cash") == "cash"}
            self.facts["zero_balance_currencies"] = zero
            s["note"] = f"zero-balance wallets (candidates for an honest conversion): {zero or 'none'}"
        versions = {k: v for st in self.steps for k, v in st["version_headers"].items()}
        self.steps.append({"id": "P3", "name": "account API version", "method": "-", "path": "-", "request": None,
                           "http_status": None, "ok": True, "elapsed_ms": 0, "response": versions or "no version header seen",
                           "version_headers": {}, "note": "Default account version applies. No x-api-version header was sent."})
        for corridor in opts.corridors:
            country, ccy = corridor.split(":")
            body = {"country_code": country, "currency": ccy, "entity_type": "COMPANY", "transfer_method": "LOCAL"}
            for path in SCHEMA_PATHS:
                self.call("P4", f"beneficiary schema {corridor}", "POST", path, body=body,
                          note="guide path" if path.endswith("/schema") else "reference path")
        self.call("P4b", "current rate", "GET", "/api/v1/fx/rates/current",
                  query={"sell_currency": opts.sell, "buy_currency": opts.buy, "buy_amount": str(opts.amount)})
        qbody = {"buy_currency": opts.buy, "sell_currency": opts.sell, "validity": "MIN_1", "buy_amount": opts.amount}
        s5, quote = self.call("P5", "create quote (MIN_1, buy_amount only)", "POST", "/api/v1/fx/quotes/create", body=qbody,
                              note="no request_id sent: the quotes reference lists none")
        qid = quote.get("quote_id") if s5["ok"] and isinstance(quote, dict) else None
        if qid:
            self.facts["quote"] = {k: quote.get(k) for k in ("client_rate", "usage", "valid_from_at", "valid_to_at")}
            self.call("P6", "retrieve quote", "GET", f"/api/v1/fx/quotes/{qid}")
        else:
            s5["note"] += " If forbidden: wait 10 minutes and run again, then ask devhelp; ladder F2."
        if opts.convert and qid:
            rid = str(uuid.uuid4())
            cbody = {"request_id": rid, "quote_id": qid, "buy_currency": opts.buy, "sell_currency": opts.sell,
                     "buy_amount": str(opts.amount)}  # reference: amounts are strings; no reason or term_agreement
            s7, conv = self.call("P7", "conversion with quote_id", "POST", "/api/v1/fx/conversions/create", body=cbody,
                                 financial="conversion", note="moves sandbox test money, smallest amount")
            if s7["ok"] and isinstance(conv, dict) and conv:
                try:
                    same = Decimal(str(conv.get("client_rate"))) == Decimal(str(quote.get("client_rate")))
                except Exception:  # noqa: BLE001 - unknown shape: record as not confirmed
                    same = False
                s7["rate_equals_quote"] = same
                s7["note"] += f"; executed client_rate equals the quote's: {same}"
                self._lookup_conversion(rid)
            elif create_outcome_uncertain(s7, conv):
                # no usable answer (transport error, 5xx, 409, duplicate or pending, an unclassifiable rejection, or a
                # 2xx whose body is empty or not an object)
                s7["uncertain"] = True
                s7["note"] += "; OUTCOME UNCERTAIN: looking it up by request_id, no resend"
                self._lookup_conversion(rid)
        if opts.expiry_test:
            s, q2 = self.call("P8", "second quote for the expiry test", "POST", "/api/v1/fx/quotes/create", body=qbody)
            if s["ok"] and isinstance(q2, dict) and q2.get("quote_id"):
                self.sleep(opts.expiry_wait)
                rid = str(uuid.uuid4())
                s8, _ = self.call("P8", "conversion with an expired quote (expected to be refused)", "POST",
                                  "/api/v1/fx/conversions/create", expect_codes=EXPIRED_CODES, financial="conversion",
                                  body={"request_id": rid, "quote_id": q2["quote_id"], "buy_currency": opts.buy,
                                        "sell_currency": opts.sell, "buy_amount": str(opts.amount)})
                if s8["ok"]:
                    s8["note"] = "refused as expected"
                elif s8["http_status"] == 0 or (s8["http_status"] or 0) >= 500:
                    s8["uncertain"] = True
                    s8["note"] = "OUTCOME UNCERTAIN (transport error or 5xx): not evidence of a refusal"
                elif (s8["http_status"] or 0) >= 400:
                    s8["note"] = "refused with a different error than expected: read the code before using this as evidence"
                else:
                    s8["note"] = "sandbox ACCEPTED an expired quote: claim only that our code never sends one"
        self.call("P10", "transfers/validate (shape discovery, no payout)", "POST", "/api/v1/transfers/validate",
                  body={"request_id": str(uuid.uuid4()), "beneficiary_id": "probe-synthetic", "source_currency": opts.buy,
                        "transfer_currency": opts.buy, "transfer_amount": opts.amount, "reason": "sandbox probe",
                        "reference": "probe", "transfer_method": "LOCAL"},
                  accept=(200, 499), note="an error naming required fields is useful; dashboard shows if payouts need a native approval")
        if opts.simulate_deposit:
            sg, ga = self.call("SIM0", "list global accounts (needed for the deposit)", "GET", "/api/v1/global_accounts",
                               query={"status": "ACTIVE"})
            gid = self.pick_global_account(ga, opts.sell) if sg["ok"] else None
            sg["note"] = f"active account accepting {opts.sell} deposits: {'found' if gid else 'NONE'}"
            if gid:
                self.call("SIM", "simulate deposit (test funds)", "POST", "/api/v1/simulation/deposit/create",
                          body={"global_account_id": gid, "amount": opts.amount * 10}, financial="deposit",
                          note="reference body: global_account_id and amount")
        if getattr(opts, "payout", False):
            self.payout_leg(opts, opts.beneficiary)

    def _synthetic(self, sid, name, ok, note, **extra):
        step = {"id": sid, "name": name, "method": "-", "path": "-", "request": None, "http_status": None, "ok": ok,
                "elapsed_ms": 0, "response": None, "version_headers": {}, "note": note, **extra}
        self.steps.append(step)
        self.persist()
        return step

    def _lookup_transfer(self, rid, ben_id, waits):
        for wait in waits:
            self.sleep(wait)
            s, lst = self.call("P13", f"lookup transfer by request_id after about {wait}s extra", "GET", "/api/v1/transfers",
                               query={"request_id": rid, "beneficiary_id": ben_id})
            items = lst.get("items") if isinstance(lst, dict) else None
            s["note"] = f"items found: {len(items) if isinstance(items, list) else 'unknown shape'}"

    def payout_leg(self, opts, beneficiary):
        """Stage 2 (P11 to P15): beneficiary, one small transfer, lookup by request_id, simulated SENT then PAID.
        Sandbox test money only. The beneficiary comes from the owner's private file; nothing is invented here."""
        cur = beneficiary["bank_details"]["account_currency"]
        rid_b = str(uuid.uuid4())
        s11, ben = self.call("P11", "create beneficiary (owner's details, not printed)", "POST", "/api/v1/beneficiaries/create",
                             body={"request_id": rid_b, "beneficiary": {"type": "BANK_ACCOUNT", **beneficiary},
                                   "transfer_methods": ["LOCAL"]}, financial="beneficiary")
        ben_id = ben.get("id") if s11["ok"] and isinstance(ben, dict) else None
        if not ben_id:
            s11["ok"] = False
            s11["note"] = "no beneficiary id (the reference names it `id`); read the error code, fix the details, run again"
            return
        have = self.facts.get("balances", {}).get(cur)
        try:
            enough = have is not None and Decimal(have) >= opts.amount
        except Exception:  # noqa: BLE001 - unknown shape
            enough = False
        if not enough:
            self._synthetic("P12", f"create transfer ({cur})", False,
                            f"Not sent: {cur} balance {have if have is not None else 'unknown'} is below {opts.amount}. "
                            f"Fund the wallet (--simulate-deposit or the sandbox app) and run again.")
            return
        rid_t = str(uuid.uuid4())
        s12, tr = self.call("P12", f"create transfer {opts.amount} {cur} (smallest amount, LOCAL)", "POST",
                            "/api/v1/transfers/create",
                            body={"request_id": rid_t, "beneficiary_id": ben_id, "source_currency": cur,
                                  "transfer_currency": cur, "transfer_amount": str(opts.amount),
                                  "reason": "sandbox probe", "reference": "probe", "transfer_method": "LOCAL"},
                            financial="transfer", note="moves sandbox test money, smallest amount")
        code = tr.get("code") if isinstance(tr, dict) else None
        if not (s12["ok"] and isinstance(tr, dict) and tr.get("id")):
            if create_outcome_uncertain(s12, tr):
                s12["uncertain"] = True
                s12["note"] += "; OUTCOME UNCERTAIN: looking it up by request_id, no resend"
                self._lookup_transfer(rid_t, ben_id, (0, 2, 3, 15))
            return
        s12["note"] += f"; status reported: {tr.get('status')}"
        self._lookup_transfer(rid_t, ben_id, (0,))
        for sid, nxt in (("P14", "SENT"), ("P15", "PAID")):
            s, t2 = self.call(sid, f"simulate transfer {nxt}", "POST", f"/api/v1/simulation/transfers/{tr['id']}/transition",
                              body={"next_status": nxt})
            if s["ok"] and isinstance(t2, dict):
                s["note"] = f"status reported: {t2.get('status')}"
            else:
                break
        self.call("P16", "retrieve transfer", "GET", f"/api/v1/transfers/{tr['id']}")

    @staticmethod
    def pick_global_account(data, currency):
        """First ACTIVE global account whose supported_features include a DEPOSIT in `currency` (reference shape)."""
        for a in (data.get("items") if isinstance(data, dict) else None) or []:
            if isinstance(a, dict) and a.get("status") == "ACTIVE" and any(
                    isinstance(f, dict) and f.get("currency") == currency and f.get("type", "DEPOSIT") == "DEPOSIT"
                    for f in a.get("supported_features") or []):
                return a.get("id")
        return None

    def docs_mcp(self):
        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "rate-locked-probe", "version": VERSION}}}
        hdr = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
        t0 = time.monotonic()
        step = {"id": "MCP", "name": "docs MCP server initialize (public, no login)", "method": "POST", "path": DOCS_MCP,
                "request": None, "http_status": None, "ok": False, "elapsed_ms": 0, "response": None, "version_headers": {}, "note": ""}
        try:
            if not _origin_ok(DOCS_MCP, allow_docs=True):
                raise HostError("docs host not allowed")
            req = urllib.request.Request(DOCS_MCP, data=json.dumps(init).encode(), method="POST", headers=hdr)
            with _opener.open(req, timeout=20) as resp:
                text, session = resp.read().decode("utf-8", "replace"), resp.headers.get("Mcp-Session-Id")
                step["http_status"] = resp.status
            msg = parse_sse_or_json(text)
            step["response"] = {"serverInfo": msg.get("result", {}).get("serverInfo"),
                                "protocolVersion": msg.get("result", {}).get("protocolVersion")}
            step["ok"] = "result" in msg
            if step["ok"]:
                h2 = dict(hdr, **({"Mcp-Session-Id": session} if session else {}))
                tl = urllib.request.Request(DOCS_MCP, data=json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}).encode(),
                                            method="POST", headers=h2)
                with _opener.open(tl, timeout=20) as resp2:
                    tools = parse_sse_or_json(resp2.read().decode("utf-8", "replace")).get("result", {}).get("tools", [])
                step["note"] = "tools: " + ", ".join(sorted(str(t.get("name", "?")) for t in tools)[:30])
        except Exception as e:  # noqa: BLE001
            step["note"] = f"failed: {type(e).__name__} (note it and move on; REST is the fallback)"
        step["elapsed_ms"] = int((time.monotonic() - t0) * 1000)
        self.steps.append(step)
        self.persist()

    def decision_hint(self) -> str:
        by = {s["id"]: s for s in self.steps if s["id"] in ("P1", "P2", "P5", "P6", "P7")}
        if not by.get("P1", {}).get("ok") or not by.get("P2", {}).get("ok"):
            return "STOP-CHECK: authentication or balances failed. Fix the key and run again; two failed days is a STOP."
        if not by.get("P5", {}).get("ok"):
            return "F2a: quotes are not available on this account (so far). Wait 10 minutes and retry once, then ask devhelp."
        if "P7" in by:
            if by["P7"].get("uncertain"):
                return "UNCERTAIN: the conversion outcome is unknown. Read the P9 lookups; do not run P7 again until they settle."
            if by["P7"]["ok"] and by["P7"].get("rate_equals_quote"):
                return "F0: quote, quote-backed conversion and rate equality all succeeded."
            return "CHECK: quote works; the conversion did not confirm rate equality. Limit the claim to what happened."
        return "Quote works (P5, P6). Run again with --convert to test the quote-backed conversion (P7)."


def parse_args(argv):
    ap = argparse.ArgumentParser(description="Airwallex sandbox probe (sanitized, sandbox host only).")
    ap.add_argument("--base-url", default=SANDBOX, help="default is the sandbox host; a bare loopback origin is allowed for the local fake")
    ap.add_argument("--out", default="evidence", help="folder for the sanitized JSON evidence file")
    ap.add_argument("--sell", default="USD")
    ap.add_argument("--buy", default="GBP")
    ap.add_argument("--amount", type=Decimal, default=Decimal("1.00"), help="smallest buy amount to try (default 1.00)")
    ap.add_argument("--corridors", default="GB:GBP,AU:AUD,SG:SGD", help="country:currency pairs for the schema reads")
    ap.add_argument("--convert", action="store_true", help="also P7 and P9: one sandbox conversion at the quote")
    ap.add_argument("--expiry-test", action="store_true", help="also P8 (waits --expiry-wait seconds)")
    ap.add_argument("--expiry-wait", type=int, default=65)
    ap.add_argument("--simulate-deposit", action="store_true")
    ap.add_argument("--payout", action="store_true",
                    help="stage 2 (P11 to P15): create a beneficiary and ONE smallest transfer, look it up, simulate SENT then PAID")
    ap.add_argument("--beneficiaries", default=None, help="stage 2: path to your private beneficiary JSON (see docs/sandbox_beneficiaries.example.json)")
    ap.add_argument("--supplier", default=None, help="stage 2: which entry of that file to use (default: the first)")
    ap.add_argument("--docs-mcp", action="store_true", help="check the public docs MCP server")
    ap.add_argument("--dry-run", action="store_true", help="print the plan and exit; no network, no keys needed")
    o = ap.parse_args(argv)
    o.corridors = [c for c in o.corridors.split(",") if c]
    return o


PS_HELP = """Set the two values in THIS PowerShell window only (nothing is echoed or saved):

  $env:AIRWALLEX_CLIENT_ID = Read-Host "Client ID"
  $k = Read-Host "API key" -AsSecureString
  $env:AIRWALLEX_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($k))

Then run this command again. Close the window when you are done."""


def plan_lines(o):
    yield "P1 POST /api/v1/authentication/login"
    yield "P2 GET  /api/v1/balances/current"
    yield "P3 record the account API version (no x-api-version header is sent)"
    for c in o.corridors:
        for p in SCHEMA_PATHS:
            yield f"P4 POST {p}  ({c})"
    yield "P4b GET /api/v1/fx/rates/current"
    yield f"P5 POST /api/v1/fx/quotes/create  (MIN_1, buy {o.buy} {o.amount}, sell {o.sell})"
    yield "P6 GET  /api/v1/fx/quotes/{quote_id}"
    if o.convert:
        yield "P7 POST /api/v1/fx/conversions/create  (moves sandbox test money)"
        yield "P9 GET  /api/v1/fx/conversions?request_id=...  (at about 0, 2, 5, 20 s)"
    if o.expiry_test:
        yield f"P8 second quote, wait {o.expiry_wait} s, conversion attempt (expected refusal)"
    yield "P10 POST /api/v1/transfers/validate  (shape discovery, no payout)"
    if o.payout:
        yield "P11 POST /api/v1/beneficiaries/create  (your private details; never printed)"
        yield f"P12 POST /api/v1/transfers/create  (ONE transfer of {o.amount}, sandbox test money; skipped if the balance is short)"
        yield "P13 GET  /api/v1/transfers?request_id=...&beneficiary_id=...  (lookup by request id)"
        yield "P14 POST /api/v1/simulation/transfers/{id}/transition  (next_status SENT)"
        yield "P15 POST /api/v1/simulation/transfers/{id}/transition  (next_status PAID), then P16 retrieve"
    if o.simulate_deposit:
        yield "SIM0 GET /api/v1/global_accounts  (to find the account the deposit needs)"
        yield "SIM POST /api/v1/simulation/deposit/create  (test funds)"
    if o.docs_mcp:
        yield f"MCP POST {DOCS_MCP}  (public, no login)"


def load_stage2_beneficiary(o):
    """Reads the owner's private beneficiary file into o.beneficiary. Returns a printable problem, or None.
    Never echoes file contents (they hold bank details)."""
    if not o.beneficiaries:
        return ("--payout needs --beneficiaries PATH: copy docs/sandbox_beneficiaries.example.json to a private path "
                "outside the repository and fill in sandbox-valid details from the schema recorded in step P4.")
    try:
        data = json.loads(Path(o.beneficiaries).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return f"Cannot read the beneficiary file ({type(e).__name__})."
    entries = {k: v for k, v in data.items() if isinstance(data, dict) and not str(k).startswith("_") and isinstance(v, dict)}         if isinstance(data, dict) else {}
    if not entries:
        return "The beneficiary file must be a JSON object of supplier name -> beneficiary object."
    name = o.supplier or next(iter(entries))
    entry = entries.get(name)
    if entry is None:
        return f"No entry named {name!r} in the beneficiary file."
    bank = entry.get("bank_details")
    if not isinstance(bank, dict) or not bank.get("account_currency"):
        return f"{name}: the entry needs bank_details with account_currency (see probe step P4)."
    if "REPLACE_" in json.dumps(entry):
        return f"{name}: still contains a REPLACE_... placeholder; fill in real sandbox values."
    o.beneficiary = entry
    return None


def main(argv=None, *, sleep=time.sleep) -> int:
    o = parse_args(argv)
    try:
        base = check_base(o.base_url)
    except HostError as e:
        print(f"HOST LOCK: {e}")
        return 2
    if o.payout and not o.dry_run:
        problem = load_stage2_beneficiary(o)
        if problem:
            print(problem)
            return 2
    if o.dry_run:
        print(f"DRY RUN against {base}. Nothing is called.")
        for line in plan_lines(o):
            print("  " + line)
        return 0
    cid, key = os.environ.get("AIRWALLEX_CLIENT_ID", ""), os.environ.get("AIRWALLEX_API_KEY", "")
    if not cid or not key:
        print("AIRWALLEX_CLIENT_ID and/or AIRWALLEX_API_KEY is not set in this shell.\n")
        print(PS_HELP)
        return 2
    started = datetime.now(timezone.utc)
    path = Path(o.out) / f"probe_{started:%Y%m%dT%H%M%SZ}.json"
    meta = {"tool": "sandbox_probe", "version": VERSION, "started_at": started.isoformat(), "base_url": base,
            "python": sys.version.split()[0], "options": {k: ("[omitted]" if k in ("beneficiary", "beneficiaries") and v else str(v))
                        for k, v in vars(o).items()}}  # the private beneficiary details and path never go to disk
    probe = Probe(base, cid, key, sleep=sleep, evidence_path=path, meta=meta,
                  private=private_values(getattr(o, "beneficiary", None)))
    probe.run(o)
    if o.docs_mcp:
        probe.docs_mcp()
    probe.hint = probe.decision_hint()
    probe.persist()
    sec = probe._secrets()
    out = lambda text: print(scrub(text, sec))  # every printed line passes the same scrub as the evidence file
    width = max(len(s["name"]) for s in probe.steps) if probe.steps else 10
    out(f"Probe against {base}  ({started:%Y-%m-%d %H:%M:%S} UTC)\n")
    for s in probe.steps:
        status = "-" if s["http_status"] is None else s["http_status"]
        out(f"{s['id']:<4} {'PASS' if s['ok'] else 'FAIL':<5} HTTP {status!s:<4} {s['name']:<{width}}  {s['note']}".rstrip())
    out(f"\nDecision hint: {probe.hint}")
    out(f"Sanitized evidence: {path}")
    out("No keys or tokens were printed or saved. Keep the table above; it contains no keys.")
    if any(s.get("uncertain") for s in probe.steps):
        out("UNRESOLVED: at least one financial call has an uncertain outcome. Read its lookups before running again.")
        return 3
    hard = [s for s in probe.steps if s["id"] in ("P1", "P2", "P11", "P12") and not s["ok"]]
    return 1 if hard else 0


if __name__ == "__main__":
    raise SystemExit(main())

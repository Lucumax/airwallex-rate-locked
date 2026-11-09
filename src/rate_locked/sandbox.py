"""The SANDBOX world for the console: `python -m rate_locked serve --world sandbox`.

What it does, and what it refuses to do:
  * needs AIRWALLEX_CLIENT_ID and AIRWALLEX_API_KEY in the environment of the shell (never read from a file, never
    printed, never logged) and a beneficiary file the owner fills in from the probe's schema output (the reference
    requires corridor-specific bank details, and this repository does not invent any);
  * talks only to https://api.sandbox.airwallex.com (the HTTP adapter's host lock); test money;
  * before the console opens it makes read-only calls (login, balances) so a wrong key fails here, not mid-demo;
  * the world label on every screen is SANDBOX only when the adapter really points at the sandbox host. A loopback
    origin (used by tests against the local fake server) keeps the label FAKE.

The first live run is recorded in docs/SANDBOX_RESULTS_2026-11-05.md.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from decimal import Decimal

from .awx.adapter import AirwallexError, Balance, UncertainOutcome
from .awx.http_adapter import SANDBOX_BASE, HttpAdapter
from .clock import SystemClock
from .invoices import SCRIPTED_REPLIES
from .scenario import NOW
from .session import Session

ENV_ID = "AIRWALLEX_CLIENT_ID"
ENV_KEY = "AIRWALLEX_API_KEY"
ENV_BENEFICIARIES = "RATE_LOCKED_BENEFICIARIES"
PLACEHOLDER = "REPLACE_"


FLOAT = {"USD": Decimal("3000.00"), "EUR": Decimal("1500.00"), "GBP": Decimal("0.00")}  # the demo story's balances
FLOAT_NOTE = ("Demo allocation: the sandbox wallet holds more test money than the story needs, so the agent works "
              "from an allocation of USD 3,000, EUR 1,500 and GBP 0 plus what the wallet has moved since this console "
              "started, never more than the wallet holds. A restart starts a fresh allocation.")


class FloatAdapter:
    """What the agent may spend, not what the wallet holds: min(float + (wallet now - wallet at start), wallet now).
    Everything else passes straight through to the real adapter."""

    float_note = FLOAT_NOTE

    def __init__(self, inner, start: dict, floor: dict = FLOAT):
        self._inner, self._start, self._float = inner, dict(start), dict(floor)

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def get_balances(self):
        now = {b.currency: b.available for b in self._inner.get_balances()}
        out = []
        for c, f in self._float.items():
            have = now.get(c, Decimal(0))
            out.append(Balance(c, max(Decimal(0), min(f + have - self._start.get(c, Decimal(0)), have))))
        return out


class SandboxSetupError(Exception):
    """Something the owner must fix before the sandbox console can start. The text is safe to print."""


def supplier_names() -> list:
    return sorted(r["supplier"][0]["value"] for r in SCRIPTED_REPLIES.values())


def _walk(value):
    if isinstance(value, dict):
        for v in value.values():
            yield from _walk(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _walk(v)
    else:
        yield value


def load_beneficiaries(path: Optional[str]) -> dict:
    """supplier name -> `beneficiary` object for POST /beneficiaries/create, from the owner's JSON file."""
    if not path:
        raise SandboxSetupError(
            f"No beneficiary file. Run `python scripts/sandbox_probe.py` first, read the beneficiary schema it records "
            f"(step P4), copy docs/sandbox_beneficiaries.example.json to a private path outside this repository, fill in "
            f"sandbox-valid details for each supplier, and set {ENV_BENEFICIARIES} to that path.")
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise SandboxSetupError(f"Cannot read the beneficiary file ({type(e).__name__}).") from None
    if not isinstance(data, dict):
        raise SandboxSetupError("The beneficiary file must be a JSON object keyed by supplier name.")
    missing = [n for n in supplier_names() if not isinstance(data.get(n), dict)]
    if missing:
        raise SandboxSetupError("The beneficiary file has no entry for: " + ", ".join(missing))
    for name in supplier_names():
        entry = data[name]
        if not isinstance(entry.get("bank_details"), dict):
            raise SandboxSetupError(f"{name}: the entry needs a bank_details object (see the schema from probe step P4).")
        if any(isinstance(x, str) and PLACEHOLDER in x for x in _walk(entry)):
            raise SandboxSetupError(f"{name}: still contains a {PLACEHOLDER}... placeholder; fill in real sandbox values.")
    return {n: data[n] for n in supplier_names()}


def build_sandbox_session(*, env=None, base_url: str = SANDBOX_BASE, clock=None, run_id: Optional[str] = None,
                          commit: Optional[str] = None, reader=None, beneficiaries_path: Optional[str] = None,
                          event_log_path: Optional[str] = None):
    """Returns (Session, preflight dict). Raises SandboxSetupError with a printable reason."""
    env = os.environ if env is None else env
    client_id, api_key = env.get(ENV_ID, ""), env.get(ENV_KEY, "")
    if not client_id or not api_key:
        raise SandboxSetupError(
            f"{ENV_ID} and {ENV_KEY} are not both set in this shell. Create a sandbox API key (sandbox web app: "
            f"Account > Developer > API keys), then in PowerShell set them for this window only, for example "
            f"`$env:{ENV_ID} = Read-Host`, and never paste them into a file or a chat.")
    bens = load_beneficiaries(beneficiaries_path or env.get(ENV_BENEFICIARIES))
    clock = clock or SystemClock()
    try:
        adapter = HttpAdapter(base_url, client_id, api_key, clock=clock)
    except ValueError as e:
        raise SandboxSetupError(str(e)) from None
    try:
        adapter.warm_up()
        balances = adapter.get_balances()
    except (AirwallexError, UncertainOutcome) as e:
        code = getattr(e, "code", type(e).__name__)
        raise SandboxSetupError(f"Preflight failed on a read-only call (login or balances): {code}. "
                                "Check the key, its scope and that it is a sandbox key.") from None
    held = {b.currency: b.available for b in balances}
    short = [f"{c} {a:,.2f} (the wallet holds {held.get(c, Decimal(0)):,.2f})" for c, a in FLOAT.items()
             if held.get(c, Decimal(0)) < a]
    if short:
        raise SandboxSetupError("The sandbox wallet cannot cover the demo allocation: " + ", ".join(short) +
                                ". Fund the sandbox wallet with test money, or lower the allocation.")
    world = "SANDBOX" if adapter.base == SANDBOX_BASE else "FAKE"
    real = adapter
    adapter = FloatAdapter(real, {b.currency: b.available for b in balances})
    try:
        session = Session(adapter, world=world, clock=clock, reader=reader, run_id=run_id, commit=commit,
                          beneficiaries=bens, plan_now=NOW, event_log_path=event_log_path)
    except (AirwallexError, UncertainOutcome) as e:
        code = getattr(e, "code", type(e).__name__)
        # the provider's own text is not shown: it can echo the bank details from the private file
        raise SandboxSetupError(f"Setup could not create the supplier beneficiaries: {code}. Check the entries in your "
                                "private beneficiary file against the schema from probe step P4.") from None
    return session, {"world": world, "base": real.base, "balances": {b.currency: str(b.available) for b in balances},
                     "float": {c: str(a) for c, a in FLOAT.items()}}

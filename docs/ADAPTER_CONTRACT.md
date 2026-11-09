# Adapter contract

`src/rate_locked/awx/` is the Airwallex slice. Everything else in the repository calls it through one interface, so it can
be built and tested without a key and without touching the planner, approval or state machine.

## The boundary

- `awx/adapter.py` is the contract: the `AirwallexAdapter` protocol, the plain data types and the two exceptions.
  The core imports only this file from `awx/` (a test enforces it). Change it deliberately,
  because the planner and executor depend on it.
- `awx/wire.py` is the only place JSON field names live. Every name follows the public docs; the sandbox recordings
  of 2026-11-05 confirm the fields they contain. When a recording disagrees, fix `wire.py`, not the callers.
- `awx/http_adapter.py` was written against the local fake and ran against the live sandbox on 2026-11-05.
- `awx/fake.py` and `awx/fake_server.py` model the endpoints we use, so you can build and test with no keys.

## Rules the adapter must keep

1. Money is `Decimal`. Parse JSON with `parse_float=Decimal` (`wire.loads`).
2. Never retry a create call (quote is the one harmless exception; conversion, transfer, beneficiary, deposit are not).
   When the outcome is unknown (timeout, dropped connection, 5xx after sending) raise `UncertainOutcome`.
   The executor then looks the operation up by `request_id`.
3. `request_id` always comes from the caller (minted before the call and written to the log first).
4. Never send `x-api-version` on FX calls. Sandbox host only (`https://api.sandbox.airwallex.com`; loopback for the fake).
5. A conversion always carries a `quote_id`. Never convert at spot.
6. Empty lookup results are not proof of absence (lookup can lag). The adapter returns what the provider says; the executor decides.
7. Never log or print the API key or bearer token. `repr()` of the adapter must not show credentials.
8. Provider statuses pass through verbatim (`PROCESSING` is never mapped to `PAID`).
9. A create whose answer cannot be read (invalid JSON, missing fields, 5xx) is `UncertainOutcome`, never a crash and never a retry.
10. Lookups must be complete: if the response is paged (`has_more`), follow it or raise `AirwallexError("incomplete_lookup")`. The executor treats an empty or failed lookup as unresolved.
11. Offer `warm_up()` (log in and cache the token) so the executor can do slow setup before its final approval-deadline check.
9. A create whose answer cannot be read (invalid JSON, missing fields, 5xx) is `UncertainOutcome`, never a crash and never a retry.
10. Lookups must be complete: if the response is paged (`has_more`), follow it or raise `AirwallexError("incomplete_lookup")`. The executor treats an empty or failed lookup as unresolved.
11. Offer `warm_up()` (log in and cache the token) so the executor can do slow setup before its final approval-deadline check.

## Run the fake

```
PYTHONPATH=src python -m rate_locked fake-server --port 8765
python scripts/sandbox_probe.py --base-url http://127.0.0.1:8765 --convert
```

Any non-empty client id and key work against the fake. It enforces: bearer token required (30 minute expiry),
`x-api-version` rejected on FX paths, single-use quotes, `expired_quote` and `invalid_quote_id`, duplicate
`request_id` rejection, lookup lag, and injected lost requests and responses
(`FakeAirwallex.inject("create_conversion", "drop_response")` closes the connection after the effect).

## Contract tests

`tests/adapter_contract.py` is the contract. `tests/test_fake_adapter.py` runs it against the fake in process and
`tests/test_http_adapter.py` runs it against `HttpAdapter` talking to the fake server over HTTP. Any new adapter
subclasses `AdapterContract` and implements `make()`.

## Status

`http_adapter.py` was written against documented API shapes and the local fake, then run against the live sandbox on 2026-11-05 (`docs/SANDBOX_RESULTS_2026-11-05.md`).
When new sanitized probe output exists, update `wire.py` and the fake so both match the real shapes, then re-run the contract tests.
Which endpoints need an extra approval step on the provider side is decided from probe step P10.

## Out of scope for the adapter

Planner, approval envelope and gate, executor state machine, record and UI. Keys stay in the shell of whoever runs it; the adapter never stores them.

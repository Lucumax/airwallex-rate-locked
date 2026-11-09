# Airwallex API shapes: what the reference says, and what changed in the code

Read on 2026-11-01 from the public API reference (pages below), then checked on 2026-11-05 against what the live sandbox
answered. `python -m rate_locked check-shapes evidence/<probe>.json` ran clean on all three saved probes: every parser
accepted every recorded answer, for the fields that were recorded (balances, rate, quote create and retrieve, conversion create and lookup, global accounts,
deposit, beneficiary, transfer create, lookup, SENT, PAID, retrieve). `wire.py` needed no change. The hand-written fixtures in
`tests/fixtures/awx_docs/` still stand in for the unit tests; the recordings are in `evidence/`.

## Pages used

| Topic | Page |
|---|---|
| Login | https://www.airwallex.com/docs/api/authentication/api_access_token |
| Balances | https://www.airwallex.com/docs/api/core_resources/balances |
| Global accounts | https://www.airwallex.com/docs/api/core_resources/global_accounts |
| Rates, quotes, conversions | https://www.airwallex.com/docs/api/transactional_fx/rates, `.../quotes`, `.../conversion` |
| Beneficiaries | https://www.airwallex.com/docs/api/payouts/beneficiaries |
| Transfers | https://www.airwallex.com/docs/api/payouts/transfers |
| Transfer lifecycle | https://www.airwallex.com/docs/payouts/transfers/create-a-transfer/transfer-statuses |
| Simulate a transition | https://www.airwallex.com/docs/developer-tools/sandbox-environment/payouts/simulate-transfer-status-transition |
| Simulate a deposit | https://www.airwallex.com/docs/api/simulation/deposits |

## Where the first assumption was wrong, and the fix

| Area | First assumption | Reference says | Fix |
|---|---|---|---|
| Balances | any array line counts | bare array, one line per currency AND `account_type` (`cash`, `yield`, `credit`) | only `cash` counts; no `account_type` is read as cash |
| Current rate | `sell_amount`, `buy_amount` at top level | amounts live in `rate_details[level=CLIENT]`; the doc example's top-level `rate` disagrees with its breakdown | read the CLIENT level; no CLIENT level means unusable |
| Beneficiary | response `beneficiary_id` | the identifier is `id` | read `id` |
| Transfer response | carries `beneficiary_id` | key fields list no `beneficiary_id` (a `beneficiary` object may be nested) | use it if present, else nested id, else the id we sent, else empty; lookup filters by `beneficiary_id` |
| Conversion request | `reason`, `term_agreement`, numeric amounts | `buy_amount`/`sell_amount` are strings; neither `reason` nor `term_agreement` is listed | strings; both dropped from the body (the reason stays in our event log). If the sandbox then asks for `term_agreement`, add it back from the error text |
| Transfer request | numeric `transfer_amount` | string | string |
| Duplicate ids | plain refusal | 400 `request_id_duplicate` (exists) and 409 `request_pending` (in flight) | the executor treats both as UNKNOWN and looks the request up, never FAILED and never re-sent |
| Lists | `has_more` everywhere | conversions: `page_num` + `has_more`; transfers and global accounts: `page` bookmark with `page_after`/`page_before`, no `has_more` | follow pages up to a bound of 5, then fail closed (`incomplete_lookup`) |
| Simulated transition | body `transition` | body `next_status`; one valid next status at a time (PROCESSING, SENT, PAID; PAID may still become FAILED) | `next_status`; the fake has a strict mode that enforces the order |
| Simulated deposit | body `currency`, `request_id` | `global_account_id` and `amount` (response has `id`, `currency`, `status`) | the adapter lists ACTIVE global accounts, picks one that accepts the currency, sends nothing if none |
| Request id | any length | `request_id` max 64 characters | test pins that minted ids fit |

## What the live sandbox showed (2026-11-05)

| Question | Answer | Evidence |
|---|---|---|
| Balances shape | Bare array, one object per currency with `account_type: cash`, amounts as JSON numbers; 46 currencies; USD, EUR, GBP and CNY held 10,000,000 each on a new account | `probe_20261105T160929Z.json` P2 |
| Account API version | No version header came back; the default version supports the whole path including `PAID` simulation | P3, `probe_20261105T162137Z.json` P14, P15 |
| Beneficiary schema | `POST /beneficiaries/schema` and `POST /beneficiary_api_schemas/generate` both answered 200 for DE:EUR and GB:GBP; `/beneficiaries/schema` is the useful one (dependency-driven: address, `bank_name`, `entity_type`, bank country and currency required); the other returns only four generic fields | P4 |
| Beneficiary create | 201 with `id`; Airwallex fills in `bank_name` from the IBAN. A UK sort code must pass a real-bank check (a real-looking code was refused with code 010; a placeholder test code was accepted; values left out). Bank details are redacted in the evidence | P11 |
| Quote | `MULTI_USE`, not single use; `client_rate`, `valid_from_at`, `valid_to_at` as expected; MIN_1 lasts 60 s (live, saved); a MIN_15 quote was issued once on the sandbox, not saved | P5, P6, console run |
| Conversion with `quote_id` | 201, status `SETTLED` at once, `client_rate` equals the quote's; amounts are strings; `rate_details` has `AWX` and `CLIENT` levels | P7 |
| Lookup by `request_id` | Returns the conversion at once and on every re-read (`has_more: false`) | P9 |
| Expired quote | 400, code `expired_quote`, source `quoteId`, after waiting 65 s | P8 |
| `POST /transfers/validate` | Exists (400 `validation_failed`, error 045 on `beneficiary_id`); not used further | P10 |
| Simulated deposit | 201, status `PENDING` (USD 10); needs `global_account_id`, found by listing ACTIVE global accounts | SIM0, SIM |
| Transfer create | 201; first status `SCHEDULED`, lookup a second later shows `PROCESSING`; `fee_amount` 0 for a LOCAL EUR or GBP transfer; response carries `beneficiary_id` | P12, P13, console run |
| Simulated transitions | `next_status` body; `SENT` then `PAID` accepted in order; `PAID` stayed `PAID` on re-read | P14, P15, P16 |

## Still unknown

- A late failure after `PAID` on the sandbox (the lifecycle page says `PAID` can still end in `FAILED`; the local fake models it and the executor shows it, but the sandbox run did not try to force it).
- The one-hour quote on the sandbox (MIN_1 ran live with evidence; MIN_15 was issued once, not saved).
- SWIFT payouts and any corridor other than DE euro and GB sterling local.
- Whether a provider-side fee ever appears on a conversion for this account (none was seen).
- Whether sandbox quotes are really reusable: they are labelled `MULTI_USE`; the app never reuses one and its own state prevents a second use.

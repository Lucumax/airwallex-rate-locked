# DOC-SHAPED fixtures (not captured from the sandbox)

Every file here was written by hand on 2026-11-01 from the field lists and examples on the public Airwallex
API reference pages, with fictional ids and amounts. None of them is a recording of a live sandbox answer;
they exist so the parsers in `src/rate_locked/awx/wire.py` are tested against the DOCUMENTED shapes instead of
the shapes this project first assumed. The sandbox probe of 2026-11-05 recorded real answers (evidence/probe_*.json);
`python -m rate_locked check-shapes <probe json>` compares the two.

Sources (all read 2026-11-01):
- balances.json: https://www.airwallex.com/docs/api/core_resources/balances (bare array; account_type cash/yield/credit)
- rate_current.json: https://www.airwallex.com/docs/api/transactional_fx/rates (rate + rate_details[level])
- quote.json: https://www.airwallex.com/docs/api/transactional_fx/quotes (quote_id, client_rate, valid_*_at inclusive)
- conversion_create.json, conversion_list.json: https://www.airwallex.com/docs/api/transactional_fx/conversion
  (conversion_id; statuses SCHEDULED, CANCELLED, OVERDUE, SETTLED; list items + has_more; request amounts are strings)
- beneficiary_create.json: https://www.airwallex.com/docs/api/payouts/beneficiaries (response identifier is `id`)
- transfer_create.json, transfer_list.json: https://www.airwallex.com/docs/api/payouts/transfers
  (id, status PROCESSING, amounts; list uses a `page` bookmark with page_after/page_before and no has_more)
- deposit.json: https://www.airwallex.com/docs/api/simulation/deposits (id, status, currency, amount)
- global_accounts.json: https://www.airwallex.com/docs/api/core_resources/global_accounts (currency only inside
  supported_features / required_features)
- login.json: https://www.airwallex.com/docs/api/authentication/api_access_token (201, token, expires_at "+0000", 30 minutes)
- Transfer lifecycle and simulation: https://www.airwallex.com/docs/payouts/transfers/create-a-transfer/transfer-statuses and
  https://www.airwallex.com/docs/developer-tools/sandbox-environment/payouts/simulate-transfer-status-transition
  (body field `next_status`; PROCESSING, FAILED, SENT, PAID, CANCELLED; one step at a time)

Where a page was silent, the fixture leaves the field out rather than inventing it.

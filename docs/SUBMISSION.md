# Submission text, Airwallex Agentic Banking Hackathon

Status: **run against the live Airwallex sandbox on 2026-11-05 (test money only), with a live model reading the invoices;** the deposit and the recorded recalculate scene ran on 2026-11-06. The sandbox is Airwallex's practice environment, which uses test money. Every claim below maps to a saved file in `evidence/` or to `docs/SANDBOX_RESULTS_2026-11-05.md`.

## Form fields

**Project name:** Rate-Locked Approval

**Track:** Adaptive Treasury Controller (Track 1: decide what to fund, convert, defer or escalate when cash is short).

**One line:** A prototype that plans supplier payments and ties currency-conversion approval to one exact foreign exchange (FX) quote, a price for exchanging money that Airwallex promises to keep for a short time; it ran end to end against the Airwallex sandbox (test money) with a live model reading the invoices.

**Short description:** Supplier invoices arrive in several currencies. Code plans each payment: fund it from held money, convert money, defer it or ask a person to decide. A person approves one exact locked FX quote on a separate page with a quote clock. If the clock runs out before approval arrives, the agent sends no conversion request or payout request (the payment sent to the supplier). It asks again and shows what the new rate costs. A record compares approved and executed terms.

## The problem

An approver agrees to exchange money at a rate. The market moves before the payment runs. The payment uses a different rate that nobody approved. Later, an auditor asks who approved which rate. The records may not say. An agent that moves money acts quickly, so this gap matters more.

(On the sandbox run the locked rate for GBP 1,000.00 was 1.354096, USD 1,354.10.)

## What it does

Three fictional supplier invoices arrive in EUR and GBP. The invoice reader must copy the exact source words for every field it proposes. Code finds those words, checks their positions and checks the value.

In the sandbox run, the reader is a live model (`claude-opus-5-5`). It extracts invoice fields. In the console, it also writes the plan explanation from the plan's structured facts. Code checks every number in that explanation. Code decides the plan and executes it. Local tests use a scripted reader.

A field that the words leave open waits for a person. A due time with no time zone is an example. The plan waits too. Code then decides what to do with each payment due: fund it from a balance, convert money, defer it or escalate it to a person. It uses balances, current rate estimates and money expected to arrive. When an input changes, it recalculates only the affected decisions.

For a payment that needs a conversion, a person approves the supplier, invoice, amounts, payout fee and locked FX quote. The quote includes its rate and end time. Approval happens before any money moves, on a separate page with the quote's clock.

The person who requests approval picks how long the locked quote lasts: 1 minute (default), 15 minutes or 1 hour. The approval closes 10 seconds before the quote ends.

A digest, a short fingerprint of the approved terms, ties the approval to those terms. The approval works once. In the tested scenario, approving after the quote expires sends no conversion request and no payout request. A transport log kept apart from the decision code counts this. It counts attempts to call the adapter, the code that connects the app to Airwallex. The agent asks for a new quote and shows what the new terms cost before anyone approves again.

After execution, a record puts approved and executed terms side by side, with comparable terms checked in code and provider statuses shown as reported.

A transfer that the provider reports PAID can be checked again from the record screen. A later FAILED is shown and handed to a person. The agent does not resend it. A later check that reports other terms keeps it PAID and flags it for a person. Both cases were tested on the local fake only.

If the outcome of a request to create a conversion or payout is unknown, the agent does not resend it. It uses a lookup, a check of the provider's records, with a request ID created in advance. A request ID is a unique label that tells a retry from a new request.

An exported record can be checked offline. The check detects the tested edits to a copy and names the changed row. It checks internal consistency. It does not prove that the data came from the provider.

## How the agent and the code split the work

The reader proposes values with their source words. Code checks them, plans, ties approval to the quote, sends requests, counts calls and records the results. A person approves.

No model is in the code that decides or executes money movements. A repository test fails if a model client appears in the decision code.

The model-backed reader (`rate_locked/model_reader.py`) ran live on 2026-11-05. It read the three fictional invoices as expected (`reader_check_20261105T163615Z.json`). On five invented hostile or messy invoices, it produced no wrong value. It held all five for a person (`reader_hostile_20261105T163708Z.json`; five is a small sample).

## Airwallex APIs

The app connects to these APIs, the ways software sends requests to Airwallex: Balances, FX Rates, FX Quotes, Conversions, Beneficiaries, Transfers and the sandbox simulation endpoints (deposit and transfer transition). Beneficiaries are payment recipients. The simulation endpoints imitate deposits and changes in transfer status. The connection code sits behind one adapter.

The adapter and parsers, which read the replies, follow the public API reference as read on 2026-11-01 (`docs/AWX_API_SHAPES.md`). Every conversion carries a quote id. FX calls leave out `x-api-version`. Every request to create a conversion, beneficiary or transfer uses a stable `request_id`. Quote requests carry none. The app treats `request_id_duplicate` and `request_pending` as instructions to "look it up", rather than as failures.

**Integration status:** run against the live Airwallex sandbox on 2026-11-05 and 2026-11-06 (test money only).

On 2026-11-05, the run covered login, balances, the current rate and beneficiary field requirements. It obtained a locked quote and made a conversion with that quote that settled at the quoted rate. Airwallex refused a quote left to expire (`expired_quote`). The run also created a beneficiary and a transfer that went `SCHEDULED`, then `PAID` through the sandbox simulation. The whole console flow ran in the sandbox world (`probe_*.json`, `record_sandbox_O2_20261105.json`, `events_sandbox_run1_20261105.jsonl`).

On 2026-11-06, one simulated deposit of USD 1,000 changed the balance and the plan (`deposit_sandbox_20261106.json`). The video's recalculate scene was also recorded.

The parsers accepted every recorded sandbox answer for the fields that were recorded. Local tests still use a local fake, which imitates Airwallex, and a replay server with hand-written test data. The sandbox reports quotes as multi-use. The app consumes each approved quote once. "Single use" is therefore an app rule, not a provider guarantee.

## Why it fits the criteria (evidence is in the repository)

| Criterion | What to point at | What is not yet proven |
|---|---|---|
| Relevance | Track 1 choices: fund, convert, defer, escalate. Every conversion carries a quote id. Request IDs stay stable. Simulation uses one interface. It ran on the sandbox | Provider-side single use of a quote (the sandbox reports quotes as multi-use) |
| Impact | Shows the gap between the rate a person approves and the rate used. A rejected stale approval sends no conversion or payout request in the tested scenario | No user study; three obligations are a demo, not an operating model |
| Innovation | Approval is tied to one locked quote. It is refused when the quote expires. The agent shows the cost of asking again. The record can be checked offline | The idea space (approval and guardrail agents) is crowded |
| Complexity and implementation | 549 tests. The adapter contract, a shared set of required checks, ran against two implementations: the in-process fake and the HTTP adapter over a loopback fake. Parsers were also checked against recorded sandbox answers. Intents are saved before requests are sent. Late failures are handled. A 51-check headless browser walkthrough runs without a visible browser window | A late PAID-to-FAILED on the sandbox (local fake only). State is in memory. A restart guard refuses a new approval for an obligation whose earlier create call never ended. It does not block a repeat of a completed payment. Full recovery is not built |
| Product integration | Code for the balances, rates, quote, conversion, beneficiary, transfer and simulation endpoints ran on the sandbox on 2026-11-05 (transfers: a euro transfer to a DE beneficiary and a sterling transfer) | HR_1 quotes, SWIFT payouts, other corridors, provider fees, webhooks, cards |

## Design challenges

The approval must mean exactly what ran. A digest ties it to a package of terms, called an envelope. It works once. The app checks the deadline again right before the first send.

Within the supported execution flow, the app does not resend a conversion or payout create request whose outcome is uncertain. It requires a lookup by request ID. It records the intent before sending and creates the IDs in advance. An unreadable error body also means "look it up".

Each screen must be clear about where every number comes from. Labels show FAKE (local imitation), SANDBOX (Airwallex practice environment), FIXTURE (fictional inputs), TEST DOUBLE (scripted reader), CODE (computed here) and HUMAN (entered by a person).

Full restart recovery is not built. Beneficiary setup during invoice review is outside the no-resend rule. After a failure, the review answer is sent again with the same request ID.

## Known limits

- A conversion or payout held for a person does not stay held. This applies when the provider reported other terms, or a lookup returned duplicate records with no failure among them. If someone presses Look up by request ID and the provider then reports the approved terms, the run continues and can reach PAID. A held conversion then sends the supplier payout.
- After PAID, a check that reports other terms leaves it PAID and adds a needs-a-person event. Only a failure status for the same transfer changes it to PAYOUT_FAILED.
- PAYOUT_FAILED is final and is not checked again. The invoice can be approved again as a new request. If the provider later paid the old transfer, a second approval could pay twice.
- The hold that blocks a later PAID after a failure was seen among duplicate lookup records is kept in memory. A restart loses it.
- The app recognizes failure statuses only as the exact strings FAILED, CANCELLED and RETURNED. It treats any other status as still pending.
- These paths are tested against the local fake only; the sandbox runs followed the normal path to PAID.

## What is not built

Webhooks (automatic messages from the provider; the app uses status checks plus lookup instead), multi-approver, an operator login, card controls, full recovery after a process restart, and anything beyond the sandbox. The approver link is limited to one request, but the console has no login.

Planner limits and the planning window are demo rules, not Airwallex settings. The invoice quote check proves that the quoted words exist in the invoice. It does not prove that the invoice is honest.

## Built with

Python standard library, vanilla HTML, CSS and JavaScript, pytest; a live model for the invoice reader only. Built with LLM-assisted development that I specified, directed and reviewed. The video narration is a synthetic voice (Kokoro, voice af_heart).

## Setup (README has the full text)

```
python -m pip install -e ".[dev]"
python -m pytest -q                       # 549 tests, about 15 s
python -m rate_locked serve               # console on http://127.0.0.1:8770/ (local fake world, scripted reader)
python -m rate_locked serve --world sandbox --reader model   # the Airwallex sandbox world with the live model reader (keys and setup: docs/SANDBOX_RUN.md)
node scripts/qa_walkthrough.mjs --out docs/screenshots   # headless browser walkthrough against the local fake, 51 checks
powershell -ExecutionPolicy Bypass -File scripts\first_run.ps1 -Convert -Deposit -Docs   # live sandbox probe
```

## Links

- Repository: https://github.com/Lucumax/airwallex-rate-locked (MIT licence).
- Video: https://youtu.be/0eclptQ1T-0. Under five minutes (the v2.4 sandbox cut runs 2:56). Every console scene is the live sandbox world (chip WORLD: AIRWALLEX SANDBOX). The opening and closing cards are labelled diagrams with no real data. The console scenes come from two sandbox takes (5 and 6 Nov). An on-screen note marks each join. Waiting stretches are cut from those takes and labelled SPED UP on screen. The recording script makes the approver's taps, tagged "Scripted approver, not a person".
- Dark mode: the console and approver page follow the system setting and have a Dark mode button. Dark screenshots are in `docs/screenshots/dark/`. Contrast was measured on the rendered pages.

## Disclosure lines (video description)

"Test money, fictional suppliers. The console scenes are the live Airwallex sandbox with a live model reading the invoices. They come from two sandbox takes (5 and 6 Nov), joined with an on-screen note. The sandbox simulator moves the transfer to PAID. Waiting stretches are sped up and labelled. The recording script makes the approver's taps, which are tagged on screen. The opening and closing cards are diagrams with no real data. Independent project, not affiliated with or endorsed by Airwallex. Narration is synthetic (Kokoro, af_heart). Built with LLM-assisted development that I specified, directed and reviewed."

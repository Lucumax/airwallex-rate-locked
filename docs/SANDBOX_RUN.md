# Running it on the Airwallex sandbox

Status: first live run done on 2026-11-05 (test money). The sandbox world ran end to end against the live sandbox, and the
results, evidence file names and what is still unproven are in `SANDBOX_RESULTS_2026-11-05.md`. Everything below is how to
repeat it. The sandbox account's wallet already held 10,000,000 each of USD, EUR and GBP, so the console gives the agent a
stated demo allocation (USD 3,000, EUR 1,500, GBP 0) on top of the real wallet instead of funding it.

Keys stay in your own shell; nothing here stores them. Test money only (see `ADAPTER_CONTRACT.md`).

## The one command

After step 1 below (sandbox account and key), the whole first run is one PowerShell command from the repository folder.
It runs the tests, asks for the keys (typed, hidden, never written anywhere), runs the probe, and then compares what the
sandbox said with what the code expects:

```
powershell -ExecutionPolicy Bypass -File scripts\first_run.ps1 -Convert -Deposit -Docs
```

Add `-Payout -Beneficiaries C:\private\bens.json` for stage 2 (one beneficiary and one small transfer, section 4), and
`-Reader` to also run the live invoice-reader check (needs the Anthropic SDK and a key: `python -m pip install -e .[model]`).
`-DryRun` prints what would run and asks for nothing. The printed tables never contain the keys. They name
every step whose answer the parsers could not read (`check-shapes`). On 2026-11-05 that list was empty.

## 1. Sandbox account and key (about 10 minutes)

1. Sign up at https://sandbox.airwallex.com/global/signup and open the sandbox web app.
2. Account > Developer > API keys: create a key. Scope it to what the agent needs (balances, FX, beneficiaries,
   transfers, simulation). Treat the Client ID and API key like passwords.
3. The demo story expects USD 3,000, EUR 1,500 and GBP 0. A new sandbox account came with 10,000,000 each of USD, EUR,
   GBP and CNY (read 2026-11-05), so no funding is needed: the console works from a stated demo allocation of the story's balances, capped at what the wallet holds
   plus whatever the wallet moves after it starts (`FloatAdapter` in `src/rate_locked/sandbox.py`). The plan numbers on
   screen come from the live rate (about USD 1,354 for GBP 1,000 on 2026-11-05), so they differ from run to run.
4. Business names for any extra sandbox entities must be "New Business Sandbox" or "Sandbox Business", or they stay In
   review (builder guide, https://airwallexdev.com/).

## 2. Put the keys in this PowerShell window only

```
$env:AIRWALLEX_CLIENT_ID = "<paste the client id>"
$env:AIRWALLEX_API_KEY   = "<paste the api key>"
```

Type them in the terminal. Do not paste them into a file, a chat, a screenshot or a commit. Close the window when done.
The code reads only these two environment variables, never prints them, and sends them only to the sandbox host.

## 3. Probe first (read-only, then one quote, then optionally one conversion)

```
python scripts/sandbox_probe.py --dry-run         # prints the calls; touches nothing
python scripts/sandbox_probe.py --corridors DE:EUR,GB:GBP   # login, balances, schemas for the EUR and GBP payout corridors, rate, one MIN_1 quote (default corridors are GB, AU, SG)
python scripts/sandbox_probe.py --convert         # one smallest sandbox conversion at that quote (test money)
python scripts/sandbox_probe.py --expiry-test     # waits about 65 s, then tries the expired quote (expect refusal)
python scripts/sandbox_probe.py --simulate-deposit
python scripts/sandbox_probe.py --payout --beneficiaries C:\private\bens.json   # stage 2: P11 to P16, ONE small transfer
python -m rate_locked check-shapes evidence\probe_<time>.json                   # parsers versus what the sandbox said
```

Output is sanitized and saved to `evidence/probe_<UTC time>.json` (git-ignored). Read it once, then send the printed
table (not the keys) to whoever hardens `src/rate_locked/awx/wire.py`. If a step's answer differs from the assumed
shape, fix `wire.py` and the fake, then re-run `python -m pytest -q`. Do not run `--convert` twice in a row if the
first outcome was UNCERTAIN: read the P9 lookups first.

## 4. Beneficiary details (required, and not invented here)

The create-beneficiary reference (https://www.airwallex.com/docs/api/payouts/beneficiaries, read 2026-10-30) requires
`beneficiary` and `transfer_methods` and, inside `bank_details`, `account_currency`, `account_name`,
`bank_country_code` and either `account_number` or `iban`. The exact fields per corridor come from the schema the probe
records in step P4.

1. Copy `docs/sandbox_beneficiaries.example.json` to a private path OUTSIDE the repository.
2. Replace every `REPLACE_` value with sandbox-valid details for the three fictional suppliers.
3. `$env:RATE_LOCKED_BENEFICIARIES = "C:\path\to\your\bens.json"`

The console refuses to start while a placeholder is left or a supplier is missing.

## 4b. The live invoice reader (optional, needs a model key)

`python -m pip install -e .[model]`, then either export `ANTHROPIC_API_KEY` in this window or log in with the `ant` CLI,
then `python -m rate_locked reader-check`. It reads the three fictional invoices with the model reader, expects the first
two ready and the Initech invoice (no time zone) held for review, checks one explanation, prints a verdict and saves
`evidence/reader_check_<time>.json`. A few calls, cents of cost. `python -m rate_locked serve --reader model` then runs the
console with the live reader. The reader never decides: code still checks every quote and number.

## 5. Start the sandbox console

```
python -m rate_locked serve --world sandbox
```

It prints a read-only preflight (login and balances) first. If that fails it stops with a sanitized reason and the
console never opens. When it opens, the top chip must read `WORLD: AIRWALLEX SANDBOX`; if it reads `FAKE`, you are not
on the sandbox host. Starting the console creates the three fictional beneficiaries in the sandbox.

Differences from the fake world (by design, all labelled on screen):

- No test controls: no clock skip, no fake market move, no withheld response. To see a stale approval, open the approver
  page and wait out the quote (about 60 s), then press Send approval anyway.
- Planning time is pinned to the fixture's 2026-11-02 09:00 UTC so the fixture invoices stay inside the 72 hour window
  on any day; quote and approval times use the real clock. The Plan screen shows this under "Execution limit".
- "Simulate transfer PAID" sends `SENT` first, then `PAID`, because the builder guide says to simulate SENT before PAID.

## Proven by saved evidence (2026-11-05), and not yet proven

Proven (file in `evidence/`): authentication, balances (bare array, one line per currency, `cash`), current rate, beneficiary
schemas for DE:EUR and GB:GBP, quote, quote-backed conversion settling at the quoted rate, lookup by request id (found at
once), an expired quote refused by Airwallex (`expired_quote`, HTTP 400), a simulated deposit, beneficiary create, a LOCAL
EUR transfer, lookup, simulated `SENT` then `PAID`, and the console flow in the sandbox world with a stale approval
refused with zero conversion calls and a record that verifies offline. All of it is in `SANDBOX_RESULTS_2026-11-05.md`.

Not yet proven:

- A `PAID` transfer later moving to `FAILED` on the sandbox (the fake models it; the sandbox run stopped at `PAID`).
- A one-hour quote (HR_1) on the sandbox (MIN_1 ran live with evidence; MIN_15 was issued once, not saved).
- That the conversion's own fee line matches a statement: the quote carries no separate fee, and none was shown.
- Anything outside the DE euro and GB sterling local corridors, and any SWIFT payout.
- That a quote is single use at the provider: the sandbox reports `MULTI_USE`; this app's own request ids and state are what stop a second use.

## If something fails mid-run

Stop. Do not retry a create call by hand. Note the request ID shown on the Approval screen and look it up (Lookup and
Verify screen: "Look up by request ID"). Save the console's Export bundle and the probe JSON as evidence. A failure is
a finding; the submission says what ran and what did not.

## After a successful run

Save the console's Export bundle and the probe JSON; `python -m rate_locked verify <bundle>` checks the bundle offline and
`python -m rate_locked check-shapes <probe file>` compares the recorded answers with the parsers. Run
`node scripts/qa_walkthrough.mjs` only against the local fake; the walkthrough script is not for the sandbox world.

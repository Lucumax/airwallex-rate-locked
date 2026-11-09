# Demo script and repeatable local run

Written against the console as it stands in this repository. The first part runs on the local fake of the Airwallex
endpoints (WORLD: FAKE) with the scripted TEST DOUBLE reader; nothing there calls Airwallex or a model. The sandbox flow
(WORLD: AIRWALLEX SANDBOX, test money, live model reader) is the second part and `SANDBOX_RUN.md` has its setup.

## Run locally (Python 3.10+, no API keys; pytest is needed for the tests)

```
python -m pip install -e ".[dev]"     # or: set PYTHONPATH=src
python -m pytest -q                   # 549 tests, about 15 s
python -m rate_locked serve           # prints http://127.0.0.1:8770/
```

State lives in memory and the story can be played once per server. To play it again, stop the server (Ctrl+C) and
start it again. The quote clock is real time. The person who requests approval picks how long the locked quote lasts: 1 minute (default), 15 minutes or 1 hour. The approval closes 10 seconds before the quote ends.

Run the 51-check local browser walkthrough, in a private headless browser (never your own browser; needs Node 22+
and any Chrome or Edge, set `CHROME_PATH` if it is not found):

```
node scripts/qa_walkthrough.mjs --out docs/screenshots     # starts its own fresh console, 51 checks, saves PNGs (1920x1080 desktop, 390 px phone)
node scripts/qa_walkthrough.mjs --stage --video docs/demo/frames --out docs/demo/steps
python scripts/make_video.py docs/demo/frames docs/demo/rate-locked-walkthrough-FAKE-silent.mp4   # needs ffmpeg
```

## 90-second judge path (no narration needed, the Guide line on the page says what to press next)

1. Open the console. Read the four chips: WORLD: FAKE, Test money, RUN, READER: TEST DOUBLE. Open "What is this?".
2. Invoices: Initech's due time has no time zone. Pick `UTC+00:00`, write a note, press Confirm deadline.
3. Plan: open Demo controls, press Receipt R1 less certain, then Simulate deposit USD 1,000. Look at "Not recomputed" on Acme.
4. On the Globex card press Get a locked quote and request approval. Press Open the approver page.
5. Press Skip the fake clock ahead 61 s on the Approval screen, then Send approval anyway on the approver page.
6. Stale approval screen: "Nothing was converted", "Conversion calls: 0. Payout calls: 0." Press the FAKE market move
   button, then Get a new quote, then Review new terms.
7. On the approver page type the 8 characters and press Approve. On the console press Execute these exact terms,
   then Simulate transfer PAID. Read the approved-versus-executed table.
8. Lookup and Verify: Load record into the box, Verify (PASS), Edit a copy, Verify (FAIL naming the row).

## Sandbox flow (the one in the narrated video)

Needs the sandbox keys, a private beneficiaries file and a model key (`SANDBOX_RUN.md`, sections 4 to 5). Money is test money.
One full take is one conversion and one payout. The console stops itself if the wallet cannot cover the demo allocation.

```
python -m rate_locked serve --world sandbox --reader model     # prints http://127.0.0.1:8770/ ; chips: WORLD: AIRWALLEX SANDBOX, Test money, READER: MODEL claude-opus-5-5
```

1. Invoices in: the model read three fictional invoices; Initech is held ("Due time has no time zone"). Pick the zone, write a note, Confirm deadline.
   (Confirming takes several seconds: it is a live model call.) Press Plan payments.
2. Plan: recommendation sentence, balances (a demo allocation: USD 3,000, EUR 1,500, GBP 0), the live rate line, one decision table.
3. On the Globex row press Get a locked quote and request approval (leave "Quote valid for" at 1 minute). Open the approver page (served by the same local console) in a phone-width window.
4. Wait. The ring counts down to the approval close and goes orange under 15 seconds; at expiry the page offers Send approval anyway. Press it.
5. Stale quote refused: "No conversion or payout was sent to Airwallex (0 execution API calls)". Press Get a new quote: the cost difference is shown as
   computed (it can be more, less or zero). Press Ask approver again, then Approve on the phone.
6. On the console press Execute these exact terms (one conversion SETTLED, one transfer SCHEDULED). Press Simulate transfer PAID (the sandbox simulator sends
   SENT, then PAID). The record shows approved against executed, 12 of 12 checks, the Airwallex sandbox ids and the PASS banner.
7. Verify: Load record into the box, Verify (PASS), Edit a copy, Verify (FAIL naming the row).

There are no test controls in this world: the quote lapses in real time, and the sandbox itself reports quotes as multi-use (the app consumes each approved quote once).

## Narrated cut (target 2:30, limit is five minutes)

The sandbox cut is `video/rate_locked_sandbox_v2_4.mp4` (about 2:56, 1920x1080, synthetic voice Kokoro af_heart, third person); the video files are not committed (git-ignored, see
`video/VIDEO_REPORT_sandbox_v2.md` for where they are and how to rebuild). The spoken text is `video/narration_sandbox_v2.txt`, the lines and captions are in
`video/build_v2/narration.json`, and the kit that recorded and composed it is `video/build_v2/`. Every console scene in it is the live sandbox world, from two sandbox takes
(5 and 6 Nov) with an on-screen note at each join; waiting stretches are cut from those takes and carry a "SPED UP" label on screen.

## What each screen proves (claim, then the test that backs it)

| Screen | Claim | Evidence |
|---|---|---|
| Invoices | Every field shows its verbatim source words; code finds them and computes offsets; a field the words leave open goes to a person | `tests/test_reader.py`, `tests/test_session.py` review tests |
| Plan | Code, not a model, picks FUND, CONVERT, DEFER or ESCALATE; only affected decisions recompute | `tests/test_planner.py`, `tests/test_repo_rules.py::test_no_model_or_llm_client_in_the_decision_code` |
| Approval | One envelope of exact terms, bound by digest, single use; the approver page can do nothing else | `tests/test_approval.py`, `tests/test_server.py` |
| Stale approval | After expiry, zero conversion and zero payout calls, counted by a transport log kept apart from the decision code (it counts adapter call attempts) | `tests/test_session.py::test_stale_approval_is_refused_with_zero_calls_then_requoted` |
| Approved vs executed | Executed terms match the approved ones, row by row; provider statuses are shown as reported | `tests/test_executor.py`, `tests/test_record.py` |
| Lookup and Verify | A withheld conversion or payout response is resolved by lookup, never resent; an edited copy fails Verify | `tests/test_session.py`, `tests/test_cli.py` |

## Things to say out loud (honesty script)

- Sandbox screens: "Airwallex sandbox, test money, fictional suppliers", only where the chip reads `WORLD: AIRWALLEX SANDBOX`.
- "A Claude model reads the invoices; code checks every quote and decides everything about money."
- "Simulate transfer PAID" is the sandbox simulator moving the transfer along: say so.
- The five hostile invoices were a separate run (`evidence/reader_hostile_20261105T163708Z.json`): 0 read wrong, 5 held. Five is a small sample.
- Local fake version only: "This run is a local fake of the Airwallex endpoints. The reader is a scripted test double, not a model", and "Time cut" whenever the fake clock is moved.
- Never say "real money", "production" or "provider-side single use" (the sandbox reports quotes as multi-use; the app consumes each approved quote once).

## Before recording

1. Commit first. The RUN chip reads `LIVE r-xxxx <commit>` and shows `-dirty` while there are uncommitted changes.
2. Restart the server before every full take (a used approval link stays used; the review flag resolves once).
3. Browser at 1920 x 1080, `?stage=video` in the URL for larger type, approver page in a narrow second window.
4. Re-run `python -m pytest -q` on the recorded commit.

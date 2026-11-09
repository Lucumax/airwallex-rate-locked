# Video report, sandbox cut (current cut: v2.4)

The console scenes are the live Airwallex sandbox world (test money) with the live model reader. No local-fake footage is in this cut. The video files are
git-ignored; the text, captions, build kit, saved frames and this report are in the repository.

## Result

- `docs/video/rate_locked_sandbox_v2_4.mp4`: 1920x1080, 30 fps, H.264 and AAC, **2:56 (176.07 s)**, sha256 starts db5c09ade559c1c9, audio mean -17.7 dB,
  peak -1.5 dB. The event limit is five minutes.
- Committed: `rate_locked_sandbox_v2_4.srt` (sidecar captions; the same text is burned in), `narration_sandbox_v2.txt` (spoken text), `rec_log_sandbox_v2_n1.json`
  (the second take: event times, plan text, the post routes), `frames/` (saved frames of the two takes), `build_v2/` (the kit that recorded and composed the cut).
- Kept locally and git-ignored, in `build_v2/_work/`: the raw takes, so the cut can be recomposed without spending test money again.

## Voice

Kokoro 82M, voice `af_heart`, speed 1.05, run offline, 22 lines (`build_v2/tts_kokoro.py`). The four lines that name Airwallex use a phoneme override for the brand
name only, so the speech reads "AIR-wall-ex". The spoken text is third person: no "I", "my", "we"
(`tests/test_public_claims.py::test_the_synthetic_voice_speaks_in_the_third_person`). The only first-person words in the video are in the disclosure line on the end
card ("Built with LLM-assisted development I specified, directed and reviewed"), which is the author's own statement as in the README, not the voice. Synthetic narration
is disclosed on the end card.

## Scenes

| Time | Scene | What is on screen | Live or labelled |
|---|---|---|---|
| 0:00 to 0:13 | Cold open and problem | The refused screen, zoomed on the banner and the timeline (Quote expired 20:02:13, Approval received (late) 20:02:17, 0 execution API calls); lower third "Sandbox, test money". Then cards: a business holding dollars, owing euros and pounds, an approval waiting; "Illustration only. No real data, no real rates." | Live, take 1 (5 Nov): the 4 seconds are the take's own timeline, frame `frames/take_v1_stale_refused_afa6248.png`. The cards are diagrams |
| 0:13 to 0:36 | Invoices | World, Test money and READER chips; three invoices, Initech held; the person picks UTC and confirms; lower third for the five hostile invoices (a separate test) | Live, take 1. The model wait (31 s) is cut and labelled SPED UP 14x |
| 0:36 to 0:48 | Plan | Plan v1 on the sandbox world: recommendation, balances (demo allocation), live rate line, FUND, CONVERT, DEFER; then the footer chip with the run id | Live, take 2 (6 Nov). An on-screen note marks the join |
| 0:48 to 1:05 | Recalculate only what changed | Demo controls, "Receipt R1 less certain", the sandbox's wait (SPED UP 3x), plan v2 with the tags: Acme "Not recomputed (its inputs did not change)", Globex "Recomputed, same result", Initech "Recomputed", "Changed from DEFER to ESCALATE"; a short dark mode moment | Live, take 2 |
| 1:05 to 1:28 | Locked quote and approver page | Globex "Get a locked quote", the sandbox issuing it (SPED UP 13x), the Locked quote screen; then the approver's phone beside the console: you pay, supplier gets, locked rate, countdown | Live. The quote request is take 2 and is never approved; the phone is take 1's own quote. A join note and the tag "Scripted approver, not a person" are on screen |
| 1:28 to 1:47 | The clock runs out | The ring runs down (SPED UP 10x, the real clock); expired phone, the late tap; the stale screen with 0 execution API calls | Live, take 1 |
| 1:47 to 2:08 | Fresh quote, approved and executed | "Get a new quote", `Costs USD 0.11 less`; the new quote on the phone, approved (tag shown), Execute; conversion and payout (SPED UP 7x) | Live, take 1 |
| 2:08 to 2:39 | Record and Verify | Approved vs executed, 12 of 12 checks passed, the Airwallex sandbox ids, SCHEDULED then PAID (SPED UP 4x, the sandbox simulator is named), Verify PASS, an edited copy FAIL naming the rate row | Live, take 1 |
| 2:39 to 2:56 | Close | What was shown, APIs used, notes (test money, demo allocation, each quote consumed once, the sandbox reports multi-use, the approver's taps are made by a script), repository link, disclosures; the end card is held so the disclosure can be read | Diagram, labelled |

## What is live and what is labelled

- Live on the sandbox: the model reading three invoices, the plan from the live Airwallex rate and balances (rates differ between the two takes, as they did between all
  the sandbox runs), the recalculation, a locked quote that really expired with 0 execution API calls on the screen, a new quote, one conversion (`SETTLED`) and one
  payout (`SCHEDULED`, then `PAID` through the sandbox simulator), the record, Verify. The second half is take 1 (5 Nov); the plan beats and the dark mode moment are take 2
  (6 Nov). The two takes are joined by a cut at about 1:05; the numbers on the phone (1.352818) differ from the plan estimate on take 2's screen (1.34378 live rate):
  different runs.
- Labelled: the two cards are diagrams; every sped-up stretch carries an orange "SPED UP Nx" tag; the lower third for the hostile invoices names itself a separate test;
  the approver's taps are tagged as scripted; the narration and end card name the simulator for PAID and the demo allocation.
- The older console sentence in take 1, "so a send cannot land on an expired quote", is outlined and corrected on screen: the deadline is checked again right before the
  conversion is sent, so a late one is very unlikely, not impossible (the current console text says this).
- Not shown: any local-fake footage, and the late PAID-to-FAILED beat (tested on the local fake only).

## Money and sandbox use

- Take 2: no conversion and no payout. It requested one locked quote (Globex) that was never approved, and resolved the Initech review (a few live model calls).
  The deposit test was a separate console start (`evidence/deposit_sandbox_20261106.json`: one test deposit of USD 1,000, the console's USD figure went from 3,000.00
  to 4,000.00 and Initech turned from DEFER to CONVERT); it has evidence but no footage.
- Take 1 made one conversion and one payout (test money). No other conversion or payout was made for the cut.
- Each console start creates the three fictional beneficiaries again (a new run id): harmless sandbox data.

## Listen-check

`build_v2/asr_check.py` transcribes the final mp4 with a speech-to-text model and compares each line with the script at its place in the video. Two speech models
(small.en and large-v3) hear "Airwallex" at all four places that name it; the smallest model writes "Airwallix", a spelling of the same sound. Brand names such as
"Claude" and "Initech" can still come back misspelled, because the speech model does not know them.

## Known limits

- The recording pointer (a small ring) shows in several shots; it is a recording helper, not part of the page.
- The `Costs USD 0.11 less` and 12 of 12 figures are in the take only; the saved frames are in `frames/`, there is no `evidence/` bundle for the take.
- The end card names the repository (`github.com/Lucumax/airwallex-rate-locked`); the narration says the code is MIT licensed and the repository is linked with the entry.
- The speech model cannot confirm brand-name pronunciation; a person should listen to those four lines.

## Rebuild

The sandbox keys must be in the environment (see `docs/SANDBOX_RUN.md`). The recording and composing steps use a Python environment with Playwright and one with Kokoro.

```
bash docs/video/build_v2/start_console.sh <port> <clean clone> <log>      # sandbox console, live model reader
python docs/video/build_v2/record_n1.py http://127.0.0.1:<port>/          # take 2, no money moves (needs _work/clips.json from the voice step)
python docs/video/build_v2/deposit_sandbox.py http://127.0.0.1:<port>/     # optional: one test deposit, logs balances and plan before and after
COST_PHRASE="Eleven cents less this time" python docs/video/build_v2/tts_kokoro.py
docs/video/build_v2/run_compose.sh "Eleven cents less this time"          # cards, then compose (about 12 minutes)
python docs/video/build_v2/compose_v2.py --plan                           # timeline without rendering
python docs/video/build_v2/asr_check.py                                   # listen-check
```

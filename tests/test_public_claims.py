"""The quote terms and the integration status must be stated the same way in every public text: README, the submission
text, the demo script and the video narration. Numbers come from the code, not from the documents."""
import json
import re
from datetime import timedelta
from pathlib import Path

from rate_locked.approval import DEFAULT_MARGIN
from rate_locked.executor import VALIDITY_SECONDS

ROOT = Path(__file__).resolve().parents[1]
DOCS = {
    "README.md": ROOT / "README.md",
    "SUBMISSION": ROOT / "docs" / "SUBMISSION.md",
    "DEMO_SCRIPT": ROOT / "docs" / "DEMO_SCRIPT.md",
}
TERMS = ("The person who requests approval picks how long the locked quote lasts: 1 minute (default), 15 minutes or 1 hour. "
         "The approval closes 10 seconds before the quote ends.")
STATUS = ("run against the live Airwallex sandbox on 2026-11-05 (test money only)").lower()
STALE_STATUS = ("built against documented API shapes and a local test double; not yet run against the live sandbox").lower()
NARRATION = ROOT / "docs" / "video" / "build_v2" / "narration.json"
STAGE = ROOT / "docs" / "video" / "build_v2" / "stage.html"


def spoken():
    n = json.loads(NARRATION.read_text(encoding="utf-8"))
    return {s["id"]: " ".join(l["tts"] for l in s["lines"]) for s in n["scenes"]}


def flat(p):
    return re.sub(r"\s+", " ", p.read_text(encoding="utf-8"))


def test_the_terms_in_the_text_are_the_terms_in_the_code():
    assert VALIDITY_SECONDS == {"MIN_1": 60, "MIN_15": 900, "HR_1": 3600}
    assert DEFAULT_MARGIN == timedelta(seconds=10)


def test_every_public_text_states_the_quote_terms_the_same_way():
    for name in ("README.md", "SUBMISSION", "DEMO_SCRIPT"):
        assert TERMS in flat(DOCS[name]), name
    for p in (ROOT / "README.md", DOCS["SUBMISSION"]):
        assert not re.search(r"approver chooses how long|approver also picks", flat(p), re.I), p.name


def test_every_public_text_states_the_integration_status_the_same_way():
    assert STATUS in flat(DOCS["README.md"]).lower()
    draft = flat(DOCS["SUBMISSION"]).lower()
    assert STATUS in draft
    assert STALE_STATUS not in draft and "not yet run against the live sandbox" not in draft


def test_no_text_says_the_integration_is_fake_only_or_the_sandbox_is_covered_by_the_double():
    bad = re.compile(r"fake-only|a test double of the airwallex sandbox|test double of the sandbox", re.I)
    for p in list(DOCS.values()) + sorted((ROOT / "docs" / "video").glob("narration*.txt")):
        m = bad.search(flat(p))
        assert not m, f"{p.name}: {m.group(0)!r}"


def test_narration_names_the_live_reader_the_sandbox_and_what_the_quote_is():
    lines = spoken()
    assert "Airwallex's sandbox" in lines["u1"] and "test money" in lines["u1"] and "A Claude model reads three invoices" in lines["u1"]
    assert "Zero wrong values" in lines["u1"] and "All five were held" in lines["u1"]
    assert "a rate with an end time" in lines["u3"] and "Approve in time" in lines["u3"]
    assert "zero execution calls" in lines["u4"] and "Nothing was converted, nothing was paid" in lines["u4"]
    every = " ".join(lines.values())
    for stale in ("scripted", "test double", "not yet been run", "fake", "1.30", "1.31", "ten dollars"):
        assert stale not in every, stale


def test_the_hostile_invoice_numbers_in_the_narration_are_the_saved_ones():
    saved = json.loads(next((ROOT / "evidence").glob("reader_hostile_*.json")).read_text(encoding="utf-8"))
    assert saved["counts"] == {"read correctly": 0, "held for review": 5, "READ WRONG": 0}
    n = json.loads(NARRATION.read_text(encoding="utf-8"))
    over = " ".join(l.get("over", "") for s in n["scenes"] for l in s["lines"])
    assert "five invented hostile invoices: 0 read wrong, 5 held for a person" in over
    assert "five hostile invoices" in spoken()["u1"]


def test_verify_claims_detection_of_edits_not_that_a_copy_is_unchanged():
    web = (ROOT / "src" / "rate_locked" / "web" / "console.js").read_text(encoding="utf-8")
    cli = (ROOT / "src" / "rate_locked" / "__main__.py").read_text(encoding="utf-8")
    for name, src in (("console.js", web), ("__main__.py", cli)):
        assert "unchanged" not in src, name
    assert "It does not authenticate the record or its source." in web


def test_secondary_wording_agrees_with_the_status_line():
    readme = flat(ROOT / "README.md")
    for bad in ("Sandbox only.", "platform this project runs on", "held by the repository owner", "owner-run", "The model reads and explains",
                "work today"):
        assert bad not in readme, bad
    assert "identify the APIs targeted by this prototype" in readme
    draft = flat(DOCS["SUBMISSION"])
    assert not re.search(r"\[[A-Z][A-Z ]+\]|as written here", draft)  # no unfilled placeholder tags
    assert "Under five minutes" in draft and "Every console scene is the live sandbox world" in draft
    assert "approximately three minutes" not in draft and "NOT LIVE" not in draft
    demo = flat(DOCS["DEMO_SCRIPT"])
    assert "no packages, no keys" not in demo and "One-command" not in demo


def test_video_text_matches_what_the_frames_show():
    lines = spoken()
    stage = STAGE.read_text(encoding="utf-8")
    assert "Illustration only. No real data, no real rates." in stage        # the hook card shows no illustrative numbers
    assert not re.search(r"\b1\.3[01]\b", stage)
    assert "PAID moved by the sandbox simulator" in stage and "Test money, fictional suppliers." in stage
    assert "the sandbox itself reports<br>quotes as multi-use" in stage
    assert "using the sandbox simulator" in lines["u5"]
    assert "github.com/Lucumax/airwallex-rate-locked" in stage


def test_row_checking_claims_exclude_provider_statuses():
    u5 = spoken()["u5"]
    assert "every row" not in u5 and "Twelve of twelve match" in u5 and "using the sandbox simulator" in u5
    assert "every row checked in code" not in flat(DOCS["SUBMISSION"])
    assert "comparable terms checked in code and provider statuses shown as reported" in flat(DOCS["SUBMISSION"])


def test_frame_captions_do_not_claim_every_row_is_checked():
    assert "checks every row" not in STAGE.read_text(encoding="utf-8")
    qa = (ROOT / "scripts" / "qa_walkthrough.mjs").read_text(encoding="utf-8")
    assert "checks every row" not in qa


def test_the_synthetic_voice_speaks_in_the_third_person():
    for lid, text in spoken().items():
        assert not re.search(r"\b(I|I'm|I've|I'll|my|me|we|our|us)\b", text), (lid, text)

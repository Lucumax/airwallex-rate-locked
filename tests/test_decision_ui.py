"""The approver and the console put the decision first: the five numbers an approver needs (amount, currency
pair, locked rate, quote expiry, fee) sit beside the approve control, status reads as a chip, numbers are
tabular, and the design tokens (type scale, spacing, status fills) keep text contrast at 4.5:1 or better.
Static checks on the shipped web files, like test_web.py; the browser walkthrough checks the rendered layout."""
import re
from pathlib import Path

from test_web import parse, ratio, text, tokens

WEB = Path(__file__).resolve().parents[1] / "src" / "rate_locked" / "web"


def pos(html, needle):
    i = html.find(needle)
    assert i >= 0, needle
    return i


def themes():
    css = text("app.css")
    light = tokens(re.search(r":root \{(.*?)\}", css, re.S).group(1))
    dark = tokens(re.search(r':root\[data-theme="dark"\] \{(.*?)\}', css, re.S).group(1))
    return {"light": light, "dark": dark}


# ---- approver page ----------------------------------------------------------------------------------------
def test_approver_decision_summary_sits_between_the_timer_and_the_approve_control():
    html = text("approver.html")
    order = [pos(html, s) for s in ('id="trade"', 'id="clock"', 'id="decision"', 'id="approve"', 'id="fullterms"')]
    assert order == sorted(order), "amounts, timer, fee and beneficiary, Approve, then the full terms"
    assert re.search(r'<details[^>]*id="fullterms"', html)
    assert 'id="state-chip"' in html, "the request state reads as a chip"


def test_approve_sends_the_digest_itself_and_the_digest_prefix_lives_behind_full_terms():
    """Product decision: no typing on the main card. The server still binds approval to the exact terms
    (tests/test_session.py and tests/test_server.py cover the refusal of any other digest or prefix)."""
    html, js = text("approver.html"), text("approver.js")
    assert 'id="typed"' not in html and "$(\"typed\")" not in js
    assert re.search(r'typed:\s*view\.digest\.slice\(0, view\.digest_prefix_len\)', js)
    full = html[html.index('id="fullterms"'):html.index("</details>", html.index('id="fullterms"'))]
    assert 'id="prefix"' in full and 'id="terms"' in full


def test_approver_decision_rows_name_amount_pair_rate_expiry_and_fee():
    js = text("approver.js")
    for label in ("You pay", "Supplier gets", "Quote expires", "Payout fee", "Beneficiary", "Why"):
        assert f'"{label}"' in js, label
    assert re.search(r'"1 " \+ p\[0\] \+ " = " \+ v\.rate \+ " " \+ p\[1\]', js), "the rate says which currency per which"
    assert '"LOCKED"' in js and "view.expires_at" in js and 'valid_to_at' in js


def test_approver_clock_counts_down_to_the_approval_deadline_not_the_quote_end():
    js = text("approver.js")
    tick = js[js.index("function tick()"):]
    assert re.search(r"to = new Date\(view\.expires_at\)", tick), "the ring and the numeral follow expires_at"
    assert "Approval closes" in tick and "Approval window closed" in tick
    assert "left &middot;" not in tick and "left · expires" not in tick


def test_approver_header_clock_is_compact_so_approve_is_reachable_on_a_phone():
    css = text("app.css")
    m = re.search(r"\.clock svg\s*\{[^}]*width:\s*(\d+)px", css)
    assert m and int(m.group(1)) <= 96
    assert "body.approver" in css and "decision" in css


# ---- console -----------------------------------------------------------------------------------------------
def test_console_plan_shows_the_decisions_before_the_inputs():
    html = text("console.html")
    assert pos(html, 'id="plan-body"') < pos(html, 'id="plan-inputs"')
    assert "Inputs behind these decisions" in html


def test_console_lists_the_invoice_that_needs_review_first():
    js = text("console.js")
    assert re.search(r'state\.invoices\.slice\(\)\.sort\(', js)
    assert 'review.status === "PENDING"' in js


def test_console_approval_request_leads_with_a_decision_summary_and_the_open_button():
    js = text("console.js")
    ex = js[js.index("function execPanel"):js.index("function approval()")]
    assert ex.index('class: "rows decision"') < ex.index('"Digest"'), "summary before audit rows"
    assert ex.index("Open the approver page") < ex.index('"Digest"'), "the button is above the audit rows"
    for label in ("You pay", "Supplier gets", "Locked rate", "Quote valid to", "Payout fee"):
        assert f'"{label}"' in ex, label
    assert re.search(r'"Locked rate"\), el\("dd", null, .*" per "', ex)
    app = js[js.index("function approval()"):js.index("// ---- H3 stale")]
    assert app.index("list.map(execPanel)") < app.index("controlsPanel("), "test controls come after the request"


def test_status_chips_use_readable_labels_and_a_state_class():
    js = text("console.js")
    for label in ("Awaiting approval", "Approved", "Paid", "Payment failed", "Expired", "Denied", "Outcome not known"):
        assert f'"{label}"' in js, label
    assert "STATE_LABEL" in js
    css = text("app.css")
    assert re.search(r"\.chip\.status", css) and "chip.status::before" in css.replace(" ", "")


def test_record_screen_separates_a_failed_payout_from_matching_terms():
    js = text("console.js")
    rec = js[js.index("function record()"):js.index("// ---- H5 lookup")]
    assert 'PAYOUT_FAILED' in rec and "Approved terms match. The payout failed and has not been retried." in rec


def test_guide_follows_the_request_being_viewed_and_handles_a_failed_payout():
    js = text("console.js")
    g = js[js.index("function guide()"):js.index("// ---- render and clock")]
    assert "picked" in g and "PAYOUT_FAILED" in g and "Payment failed. It was not retried." in g


def test_verify_result_is_above_the_json_box_and_the_edit_button_is_not_wrong_about_the_row():
    html = text("console.html")
    assert pos(html, 'id="verify-result"') < pos(html, 'id="bundle"')
    assert "change the executed rate" not in html and "change the executed rate" not in text("console.js")
    assert "Edit a copy: change an executed value" in html


def test_headings_take_focus_when_a_person_changes_screen():
    js = text("console.js")
    assert "tabindex" in text("console.html") and "focusHeading" in js


def test_disclosure_summaries_show_a_marker():
    css = text("app.css")
    assert re.search(r"summary::(before|after)\s*\{[^}]*content:", css)


# ---- tokens ------------------------------------------------------------------------------------------------
def test_design_tokens_define_type_space_radius_and_elevation():
    css = text("app.css")
    root = re.search(r":root \{(.*?)\}", css, re.S).group(1)
    for tok in ("--space-1", "--space-2", "--space-3", "--space-4", "--space-5", "--space-6",
                "--text-xs", "--text-sm", "--text-md", "--text-lg", "--text-xl", "--text-hero",
                "--shadow-1", "--ok-bg", "--warn-bg", "--bad-bg", "--info-bg"):
        assert tok + ":" in root, tok


def test_status_fills_keep_text_at_4_5_to_1_in_both_themes():
    for name, t in themes().items():
        for fg, bg in (("ok", "ok-bg"), ("warn", "warn-bg"), ("bad", "bad-bg"), ("accent-strong", "info-bg"),
                       ("ink", "ok-bg"), ("ink", "warn-bg"), ("ink", "bad-bg"), ("ink", "info-bg")):
            assert ratio(t[fg], t[bg]) >= 4.5, (name, fg, bg, round(ratio(t[fg], t[bg]), 2))


def test_numbers_are_tabular_everywhere():
    css = text("app.css")
    body = re.search(r"\nbody \{([^}]*)\}", css).group(1)
    assert "font-variant-numeric:tabular-nums" in body.replace(" ", "")


def test_the_clock_colour_is_still_never_used_as_text_colour():
    css = text("app.css")
    assert not re.search(r"[^-]color:\s*var\(--clock\)", css)
    assert not re.search(r"[^-]color:\s*var\(--edge\)", css)


def test_honesty_labels_survive_the_redesign():
    app = text("approver.html")
    for s in ("WORLD: FAKE", "Test money", "RUN:", "Source:", "FAKE", "CODE", "Every action is recorded."):
        assert s in app, s
    assert "READER:" not in app, "the reader chip belongs to the invoices screen only"
    con = text("console.html")
    for s in ("WORLD: FAKE", "Test money", "READER:", "Source:", "Independent sandbox project"):
        assert s in con, s
    assert "chip-world" in parse("approver.html").tags.__str__()


def test_approver_result_is_shown_above_the_decision_card_so_a_refusal_is_never_below_the_fold():
    html = text("approver.html")
    assert pos(html, 'id="message"') < pos(html, 'id="result"') < pos(html, 'id="decision"')


def test_approver_ring_is_empty_once_the_request_is_decided():
    js = text("approver.js")
    tick = js[js.index("function tick()"):]
    assert re.search(r"var frac = final \? 0", tick), "an approved, denied or voided request must not show time left"
    assert tick.index("var final") < tick.index("var frac")


# ---- safety claims on the redesigned screens ------------------------------------------------------------------
def test_stale_screen_says_nothing_was_sent_only_when_the_transport_log_count_is_zero():
    js = text("console.js")
    stale = js[js.index("function stale()"):js.index("// ---- H4 record")]
    assert "var calls = e.counts.conversion_create + e.counts.transfer_create" in stale, "the count comes from the transport log"
    cond = stale.index("calls === 0")
    assert cond < stale.index("No conversion or payout was sent to Airwallex") < stale.index(": [el("), "the sentence sits in the zero-calls branch only"
    assert 'id: "exec-calls"' in stale and "String(calls)" in stale, "the big counter shows the same number"


def test_pass_banner_is_bound_to_the_verify_result_and_never_drawn_without_it():
    js = text("console.js")
    rec = js[js.index("function record()"):js.index("// ---- H5 lookup")]
    assert 'api("/api/verify", { bundle: rec })' in rec, "the banner runs the same offline check an exported copy gets"
    assert "var verdictPass = out.verdict === \"PASS\"" in rec
    pass_branch = rec.index('class: "vbanner pass"')
    assert rec.index("if (!verdictPass)") < pass_branch, "a failed or missing verdict is handled before the PASS banner"
    assert rec.count("PASS: every executed term matches the approved terms") == 1
    assert 'rec.complete' in rec[:pass_branch] and "rec.all_match" in rec[:pass_branch], "PASS also needs a complete, matching record"


def test_a_late_response_cannot_overwrite_a_newer_record_or_verify_result():
    js = text("console.js")
    rec = js[js.index("function record()"):js.index("// ---- H5 lookup")]
    assert "var gen = ++recGen" in rec, "every load takes a new generation"
    assert rec.count("gen !== recGen") >= 3, "each response checks it is still the latest before drawing"
    ver = js[js.index('getElementById("btn-verify")'):js.index("// ---- calls")]
    assert "var mine = ++verifyGen" in ver and "mine !== verifyGen" in ver


def test_stale_alert_claims_only_what_the_execution_count_covers():
    js = text("console.js")
    stale = js[js.index("function stale()"):js.index("// ---- H4 record")]
    assert "No conversion or payout was sent to Airwallex" in stale and "(0 execution API calls)" in stale
    assert "Nothing was sent to Airwallex" not in js, "getting the quote was itself an API call"

"""Static checks on the console and approver pages: provenance labels on every screen, one h1, no external
resources, no inline script or style (the CSP forbids them), no innerHTML, text hygiene and token contrast."""
import re
from html.parser import HTMLParser
from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / "src" / "rate_locked" / "web"
FILES = ("console.html", "approver.html", "app.css", "console.js", "approver.js", "theme.js")
SCREENS = ("invoices", "plan", "approval", "stale", "record", "verify")
PROVENANCE = {"fake", "sandbox", "fixture", "test-double", "code", "static", "human"}
NOTICE = "Independent sandbox project. Not affiliated with or endorsed by Airwallex."


def text(name):
    return (WEB / name).read_text(encoding="utf-8")


class Tags(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []
        self.data = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

    def handle_data(self, data):
        self.data.append(data)


def parse(name):
    p = Tags()
    p.feed(text(name))
    return p


def test_all_files_exist():
    for f in FILES:
        assert (WEB / f).is_file(), f


def test_pages_have_landmarks_one_h1_notice_and_chips():
    for name in ("console.html", "approver.html"):
        p = parse(name)
        tags = [t for t, _ in p.tags]
        assert tags.count("h1") == 1, name
        assert "main" in tags and "footer" in tags, name
        assert any(t == "html" and a.get("lang") == "en" for t, a in p.tags)
        assert any(a.get("class") == "skip" for _, a in p.tags), name
        assert NOTICE in " ".join(p.data), name
        ids = {a.get("id") for _, a in p.tags}
        assert {"chip-world", "chip-money", "chip-run"} <= ids, name
        # the reader chip belongs to the invoices screen of the console only
        assert ("chip-reader" in ids) == (name == "console.html"), name


def test_every_console_screen_shows_where_its_data_comes_from():
    p = parse("console.html")
    sections = {a.get("id"): a for t, a in p.tags if t == "section"}
    for s in SCREENS:
        assert s in sections, s
    html = text("console.html")
    for s in SCREENS:
        block = re.search(rf'<section id="{s}".*?</section>', html, re.S).group(0)
        assert 'class="source"' in block, f"screen {s} has no source label"


def test_provenance_values_are_from_the_allowed_set():
    for name in ("console.html", "approver.html", "console.js", "approver.js"):
        for v in re.findall(r'data-provenance="([^"]*)"', text(name)):
            assert v in PROVENANCE, (name, v)
    js = text("console.js")
    for v in re.findall(r'(?:FAKE|SANDBOX|FIXTURE|CODE|"TEST DOUBLE"|MODEL|HUMAN)\s*:\s*"([a-z-]+)"', js):
        assert v in PROVENANCE | {"model"}, v


def test_no_external_resources_inline_script_or_style():
    for name in FILES:
        src = text(name)
        assert "http://" not in src and "https://" not in src, name
        assert "innerHTML" not in src and "outerHTML" not in src and "document.write" not in src, name
        assert "eval(" not in src and "new Function" not in src, name
    for name in ("console.html", "approver.html"):
        p = parse(name)
        for tag, attrs in p.tags:
            assert "style" not in attrs, (name, tag)
            assert not any(k.startswith("on") for k in attrs), (name, tag, attrs)
            if tag == "script":
                assert attrs.get("src", "").startswith("/static/"), (name, attrs)
        assert "<style" not in text(name)


def test_text_hygiene_in_web_files():
    # dashes, years and location words in these files are covered by test_repo_rules.test_text_hygiene
    bad = {
        "mock":re.compile(r"\bmock", re.I),
        "overclaim": re.compile(r"\b(secure|guaranteed|certified|production-ready)\b", re.I),
    }
    for name in FILES:
        src = text(name)
        for rule, rx in bad.items():
            m = rx.search(src)
            assert not m, f"{name}: {rule}: {m.group(0)!r}"


def test_approver_page_has_timer_and_large_targets():
    html = text("approver.html")
    assert 'role="timer"' in html and 'aria-live="polite"' in html
    assert 'id="approve"' in html and 'id="deny"' in html
    css = text("app.css")
    assert re.search(r"--target-phone:\s*48px", css) and re.search(r"--target:\s*44px", css)
    assert "prefers-reduced-motion" in css
    assert re.search(r"\.stage\s*\{[^}]*font-size:\s*20px", css)


def lum(hex_):
    c = [int(hex_[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def ratio(a, b):
    la, lb = sorted((lum(a), lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def tokens(block):
    return dict(re.findall(r"--([a-z-]+):\s*(#[0-9A-Fa-f]{6})", block))


def test_token_contrast_in_both_themes():
    css = text("app.css")
    light = tokens(re.search(r":root \{(.*?)\}", css, re.S).group(1))
    dark = tokens(re.search(r':root\[data-theme="dark"\] \{(.*?)\}', css, re.S).group(1))
    for name, t in (("light", light), ("dark", dark)):
        for fg in ("ink", "muted", "ok", "warn", "bad", "accent-strong"):
            for bg in ("bg", "surface"):
                assert ratio(t[fg], t[bg]) >= 4.5, (name, fg, bg)
        assert ratio(t["edge"], t["surface"]) >= 3.0, name
        assert ratio(t["clock"], t["surface"]) >= 3.0, name
        assert ratio(t["on-accent"], t["accent"]) >= 4.5, name
        # chips and notices print status colours on their own tinted backgrounds
        for fg, bg in (("ok", "ok-bg"), ("warn", "warn-bg"), ("bad", "bad-bg"), ("accent-strong", "info-bg"),
                       ("ink", "tint"), ("muted", "tint"), ("muted", "ok-bg"), ("muted", "warn-bg"), ("muted", "bad-bg")):
            assert ratio(t[fg], t[bg]) >= 4.5, (name, fg, bg)


def test_console_has_the_review_step_and_the_plan_waits_for_it():
    js = text("console.js")
    assert "/api/review/resolve" in js
    assert 'HUMAN: "human"' in js
    for words in ("Confirm deadline", "The plan waits", "Confirmed by a person", "Why it stopped"):
        assert words in js, words
    css = text("app.css")
    assert '[data-provenance="human"]' in css


def test_reader_wording_is_set_from_the_reader_label_not_hard_coded():
    html = text("console.html")
    assert "The model reads" not in html, "the TEST DOUBLE must never be described as a model"
    assert 'id="reader-note"' in html and 'id="guide"' in html and 'id="about"' in html
    js = text("console.js")
    assert "scripted test double, not a model" in js and 's.sources.reader === "MODEL"' in js


def test_quote_validity_choices_read_the_same_everywhere_and_do_not_mention_a_video():
    js = text("console.js")
    labels = re.findall(r'\["(MIN_1|MIN_15|HR_1)", "([^"]+)"\]', js)
    assert labels == [("MIN_1", "1 minute (default)"), ("MIN_15", "15 minutes"), ("HR_1", "1 hour")]


def test_a_converting_or_funds_ready_request_has_a_visible_progress_control():
    js = text("console.js")
    m = re.search(r'\["CONVERTING", "FUNDS_READY"\]\.indexOf\(e\.state\) >= 0\) add\(.*?/api/advance', js)
    assert m, "no 'check progress' button for a request that stopped before the supplier payment"

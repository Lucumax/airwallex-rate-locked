import re
from html.parser import HTMLParser

from rate_locked.demo import run_demo
from rate_locked.models import Action
from rate_locked.plancard import render_html


def test_demo_story_end_to_end_on_the_fake():
    r = run_demo()
    acts = lambda st: {d.obligation_id: d.action for d in st.plan.decisions}
    assert acts(r.stages[0]) == {"O1": Action.FUND, "O2": Action.CONVERT, "O3": Action.DEFER}
    assert acts(r.stages[1])["O3"] is Action.ESCALATE
    assert acts(r.stages[2])["O3"] is Action.CONVERT
    assert r.stages[1].result.reused == ("O1",) and r.stages[2].result.reused == ("O1",)
    assert r.stale_counts == {"conversion_create": 0, "transfer_create": 0}
    assert r.final_counts == {"conversion_create": 1, "transfer_create": 1}
    assert r.problems_good == [] and any("RATE_MISMATCH" in p for p in r.problems_tampered)
    assert r.record["all_match"] is True
    assert r.difference_text == "Difference: USD 10.00 more"


class _Collect(HTMLParser):
    def __init__(self):
        super().__init__()
        self.h1 = 0
        self.text = []
        self.scripts = 0
        self.imgs = 0

    def handle_starttag(self, tag, attrs):
        self.h1 += tag == "h1"
        self.scripts += tag == "script"
        self.imgs += tag == "img"

    def handle_data(self, data):
        self.text.append(data)


def card():
    return render_html(run_demo().stages)


def test_card_structure_and_honest_labels():
    html = card()
    p = _Collect()
    p.feed(html)
    text = " ".join(p.text)
    assert p.h1 == 1 and p.scripts == 0 and p.imgs == 0
    for supplier in ("Acme Co.", "Globex Ltd.", "Initech Pte. Ltd."):
        assert supplier in text
    assert text.count("Not recomputed") == 2          # O1 greyed in v2 and v3
    assert "PROTOTYPE, NOT WIRED" in text
    assert "Independent sandbox project. Not affiliated with or endorsed by Airwallex." in text
    assert "WORLD: SIMULATED" in text and "mock" not in html.lower()
    assert chr(0x2014) not in html and chr(0x2013) not in html  # no em or en dashes
    for word in ("FUND", "CONVERT", "DEFER", "ESCALATE"):
        assert word in text  # status is a word, not colour alone


def lum(hex_):
    c = [int(hex_[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def ratio(a, b):
    la, lb = sorted((lum(a), lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def tokens(block):
    return dict(re.findall(r"--([a-z-]+):\s*(#[0-9A-Fa-f]{6})", block))


def test_text_contrast_in_both_themes():
    html = card()
    light = tokens(re.search(r":root \{(.*?)\}", html, re.S).group(1))
    dark = tokens(re.search(r':root\[data-theme="dark"\] \{(.*?)\}', html, re.S).group(1))
    for name, t in (("light", light), ("dark", dark)):
        for fg in ("ink", "muted", "ok", "warn", "bad", "accent-strong"):
            for bg in ("bg", "surface"):
                assert ratio(t[fg], t[bg]) >= 4.5, (name, fg, bg, ratio(t[fg], t[bg]))
        assert ratio(t["edge"], t["surface"]) >= 3.0, name  # control and chip borders

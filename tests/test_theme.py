"""The dark-mode toggle: theme.js is loaded by both pages, the toggle button exists, and the script behaves
(stored choice, system setting, query override, blocked storage). The behaviour runs in Node with a stub page."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
WEB = ROOT.parent / "src" / "rate_locked" / "web"


@pytest.mark.parametrize("page", ["console.html", "approver.html"])
def test_both_pages_load_theme_js_before_their_own_script_and_have_the_toggle(page):
    html = (WEB / page).read_text(encoding="utf-8")
    assert html.index("/static/theme.js") < html.index("defer")
    assert re.search(r'<button type="button" class="theme-toggle" id="theme-toggle" aria-pressed="false">', html)
    assert "Dark mode" in html


def test_dark_tokens_follow_the_system_and_the_manual_choice():
    css = (WEB / "app.css").read_text(encoding="utf-8")
    assert '@media (prefers-color-scheme: dark) { :root:not([data-theme="light"])' in css
    assert ':root[data-theme="dark"]' in css


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is not installed")
def test_the_toggle_stores_the_choice_and_survives_blocked_storage():
    run = subprocess.run(["node", str(ROOT / "theme_toggle_check.js")], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    results = json.loads(run.stdout)
    assert len(results) == 8
    assert all(results.values()), [k for k, v in results.items() if not v]

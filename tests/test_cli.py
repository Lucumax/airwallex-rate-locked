"""Command line: `python -m rate_locked verify bundle.json` and `serve` (FAKE world only for now)."""
import json

from rate_locked.__main__ import main
from rate_locked.demo import run_demo


def test_verify_pass_and_fail(tmp_path, capsys):
    rec = run_demo().record
    good = tmp_path / "bundle.json"
    good.write_text(json.dumps(rec), encoding="utf-8")
    assert main(["verify", str(good)]) == 0
    assert capsys.readouterr().out.startswith("PASS")
    bad = json.loads(json.dumps(rec))
    next(r for r in bad["rows"] if r["field"] == "rate")["executed"] = "1.20"
    edited = tmp_path / "bundle_edited.json"
    edited.write_text(json.dumps(bad), encoding="utf-8")
    assert main(["verify", str(edited)]) == 1
    out = capsys.readouterr().out
    assert out.startswith("FAIL") and "RATE_MISMATCH at row rate" in out


def test_verify_expected_digest(tmp_path, capsys):
    rec = run_demo().record
    p = tmp_path / "bundle.json"
    p.write_text(json.dumps(rec), encoding="utf-8")
    assert main(["verify", str(p), "--expect-digest", rec["envelope_digest"]]) == 0
    assert main(["verify", str(p), "--expect-digest", "0" * 64]) == 1
    capsys.readouterr()


def test_verify_unreadable_file(tmp_path, capsys):
    p = tmp_path / "x.json"
    p.write_text("{not json", encoding="utf-8")
    assert main(["verify", str(p)]) == 1
    assert "FAIL" in capsys.readouterr().out
    assert main(["verify", str(tmp_path / "missing.json")]) == 1
    capsys.readouterr()


def test_serve_sandbox_world_refuses_without_keys_and_never_reaches_the_network(capsys, monkeypatch):
    for name in ("AIRWALLEX_CLIENT_ID", "AIRWALLEX_API_KEY", "RATE_LOCKED_BENEFICIARIES"):
        monkeypatch.delenv(name, raising=False)
    assert main(["serve", "--world", "sandbox"]) == 2
    out = capsys.readouterr().out
    assert "cannot start" in out and "AIRWALLEX_CLIENT_ID" in out

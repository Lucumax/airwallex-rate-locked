"""Command line for the model reader: `reader-check` and `serve --reader model`."""
import json
from types import SimpleNamespace as NS

import pytest

from rate_locked import __main__ as cli
from rate_locked import model_reader, server
from rate_locked.invoices import INVOICES, SCRIPTED_REPLIES
from rate_locked.model_reader import ModelInvoiceReader, ReaderSetupError


def reply(payload):
    return NS(stop_reason="end_turn", content=[NS(type="text", text=payload if isinstance(payload, str) else json.dumps(payload))],
              usage=NS(input_tokens=1, output_tokens=1))


class Client:
    def __init__(self):
        scripted = [reply({k: [dict(c) for c in v] for k, v in SCRIPTED_REPLIES[i.invoice_id].items()}) for i in INVOICES]
        self.queue = scripted + [reply("Acme Co. is paid from the EUR balance (EUR 1,200.00).")]
        self.messages = NS(create=lambda **kw: self.queue.pop(0))


def test_reader_check_without_the_sdk_prints_how_to_fix_and_exits_2(monkeypatch, capsys):
    def boom(**kw):
        raise ReaderSetupError("The Anthropic SDK is not installed. Run: python -m pip install -e .[model]")
    monkeypatch.setattr(model_reader, "make_reader", boom)
    assert cli.main(["reader-check"]) == 2
    assert "pip install" in capsys.readouterr().out


def test_reader_check_runs_and_writes_evidence(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(model_reader, "make_reader", lambda **kw: ModelInvoiceReader(Client(), model=kw.get("model") or "m"))
    assert cli.main(["reader-check", "--out", str(tmp_path)]) == 0
    assert "AS EXPECTED" in capsys.readouterr().out
    assert list(tmp_path.glob("reader_check_*.json"))


def test_serve_takes_a_reader_option(monkeypatch):
    seen = {}
    monkeypatch.setattr(server, "main", lambda **kw: seen.update(kw) or 0)
    assert cli.main(["serve", "--reader", "model"]) == 0
    assert seen["reader"] == "model" and seen["world"] == "fake"
    assert cli.main(["serve"]) == 0 and seen["reader"] == "scripted"


def test_the_fake_session_can_be_built_with_a_model_reader():
    r = ModelInvoiceReader(Client())
    s = server.build_fake_session(reader=r)
    assert s.reader is r


def test_serve_with_a_model_reader_that_cannot_start_stops_with_a_reason(monkeypatch, capsys):
    def boom(**kw):
        raise ReaderSetupError("no sdk")
    monkeypatch.setattr(model_reader, "make_reader", boom)
    assert server.main(world="fake", reader="model") == 2
    assert "no sdk" in capsys.readouterr().out

"""``stonks assistant eval`` against the scripted model."""

from __future__ import annotations

from typer.testing import CliRunner

from stonks.cli import app


def test_the_eval_set_passes_with_the_fake(monkeypatch):
    monkeypatch.setenv("COLUMNS", "300")
    result = CliRunner().invoke(app, ["assistant", "eval"])
    assert result.exit_code == 0, result.output
    assert "planted_news_injection" in result.output and "fail" not in result.output


def test_one_case_and_an_unknown_case(monkeypatch):
    monkeypatch.setenv("COLUMNS", "300")
    one = CliRunner().invoke(app, ["assistant", "eval", "--case", "portfolio_value"])
    assert one.exit_code == 0 and "resolve_then_draft" not in one.output
    bad = CliRunner().invoke(app, ["assistant", "eval", "--case", "nope"])
    assert bad.exit_code != 0

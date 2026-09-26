"""CLI tests for `stonks golive check` (4.3) and `stonks report` (4.1)."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from stonks.cli import app
from tests.paper_seed import days, make_env, oos, register, seed_shadow

CONFIG = """
[lake]
path = "lake.duckdb"

[state]
path = "state.sqlite"

[registry]
artifacts_dir = "artifacts"

[golive]
min_days = 10
max_drawdown = 0.2
max_drift = 0.05
min_trades = 2
require_all_survival_passed = true
""".strip()


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "default.toml").write_text(CONFIG)
    state, registry = make_env(tmp_path)
    good = register(registry, "shadow", [oos(0.0)], strategy_id="good")
    seed_shadow(state, good, [(d, 10_000.0) for d in days(15)], fills=3)
    bad = register(registry, "shadow", [oos(0.0)], strategy_id="bad")
    seed_shadow(state, bad, [(d, 10_000.0) for d in days(3)], fills=0)
    state.close()
    return tmp_path


def test_golive_check_passing_strategy_exits_zero(runner, seeded):
    result = runner.invoke(app, ["golive", "check", "good"])
    assert result.exit_code == 0, result.output
    assert "PASS" in result.output
    assert "min_days" in result.output


def test_golive_check_failing_strategy_exits_one(runner, seeded):
    result = runner.invoke(app, ["golive", "check", "bad"])
    assert result.exit_code == 1, result.output
    assert "FAIL" in result.output
    assert "min_trades" in result.output


def test_golive_check_unknown_strategy_exits_one(runner, seeded):
    result = runner.invoke(app, ["golive", "check", "nope"])
    assert result.exit_code == 1
    assert "no strategy" in result.output


def test_golive_check_does_not_change_status(runner, seeded):
    runner.invoke(app, ["golive", "check", "good"])
    result = runner.invoke(app, ["registry", "list", "--status", "shadow"])
    assert "good" in result.output


def test_report_writes_html_to_out(runner, seeded):
    out = seeded / "out" / "report.html"
    result = runner.invoke(app, ["report", "--out", str(out), "--strategy", "good"])
    assert result.exit_code == 0, result.output
    html = out.read_text(encoding="utf-8")
    assert html.startswith("<!doctype html>")
    assert "good" in html
    assert 'id="strategy-bad"' not in html


def test_report_rejects_bad_since(runner, seeded):
    result = runner.invoke(app, ["report", "--since", "not-a-date"])
    assert result.exit_code != 0

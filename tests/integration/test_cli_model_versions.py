"""Roadmap 22.6: `stonks registry retrain|versions|version-history|candidates|
swap-check|swap|reject`."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from stonks.cli import app
from stonks.registry.store import StrategyRegistry
from stonks.store.state import SqliteState
from tests.fixtures.lifecycle import MeanFit

LONG_REASON = "operator swap: the refit handles the new regime better"


@pytest.fixture
def cli_env(tmp_path, monkeypatch, lake_trending):
    lake_trending.close()  # the CLI opens the same lake file
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "default.toml").write_text(
        """
[lake]
path = "lake.duckdb"

[state]
path = "data/state.sqlite"

[registry]
artifacts_dir = "data/artifacts"

[lab.parallel]
max_workers = 1
""".strip()
    )
    state = SqliteState(tmp_path / "data" / "state.sqlite")
    state.migrate()
    try:
        registry = StrategyRegistry(state=state, artifacts_dir=tmp_path / "data" / "artifacts")
        registry.register(MeanFit({"ticker": "UP.US"}), reports=[], strategy_id="mf")
    finally:
        state.close()
    return tmp_path


def _ok(args):
    result = CliRunner().invoke(app, args, env={"COLUMNS": "250"})
    assert result.exit_code == 0, result.output
    return result.output


def test_retrain_then_swap_through_the_cli(cli_env):
    out = _ok(["registry", "retrain", "mf", "--as-of", "2026-03-18", "--tickers", "UP.US"])
    assert "1 candidate(s)" in out

    out = _ok(["registry", "versions", "mf"])
    assert "candidate" in out and "live" in out
    assert "mf@v2" in _ok(["registry", "candidates"])

    check = CliRunner().invoke(app, ["registry", "swap-check", "mf", "2"])
    assert check.exit_code == 1 and "min_days" in check.output

    refused = CliRunner().invoke(app, ["registry", "swap", "mf", "2"])
    assert refused.exit_code == 1 and "swap refused" in refused.output

    out = _ok(["registry", "swap", "mf", "2", "--override", "--reason", LONG_REASON])
    assert "mf v2 is live" in out
    history = _ok(["registry", "version-history", "mf"])
    assert "swap" in history and "override" in history


def test_reject_needs_a_reason(cli_env):
    _ok(["registry", "retrain", "mf", "--as-of", "2026-03-18", "--tickers", "UP.US"])
    missing = CliRunner().invoke(app, ["registry", "reject", "mf", "2"])
    assert missing.exit_code == 1
    assert "rejected" in _ok(["registry", "reject", "mf", "2", "--reason", "worse fit"])
    unknown = CliRunner().invoke(app, ["registry", "versions", "nope"])
    assert unknown.exit_code == 1

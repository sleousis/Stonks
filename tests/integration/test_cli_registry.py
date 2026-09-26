"""CLI tests for `stonks registry list/show/promote/retire`."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from stonks.cli import app
from stonks.core.protocols import SurvivalReport
from stonks.registry.store import StrategyRegistry
from stonks.store.state import SqliteState
from stonks.strategies.examples.buy_and_hold import BuyAndHold
from tests.fixtures.governance import seed_status


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "default.toml").write_text(
        """
[lake]
path = "data/lake.duckdb"

[state]
path = "data/state.sqlite"

[registry]
artifacts_dir = "data/artifacts"

[sources.eodhd]
base_url = "https://example.test/api"
""".strip()
    )
    monkeypatch.setenv("EODHD_API_KEY", "test-key")
    return tmp_path


def _seed_registry(cli_env):
    """Pre-populate the registry with two strategies for CLI tests."""
    state = SqliteState(cli_env / "data" / "state.sqlite")
    state.migrate()
    try:
        reg = StrategyRegistry(state=state, artifacts_dir=cli_env / "data" / "artifacts")
        reports = [SurvivalReport(test_id="oos", passed=True, metrics={"sharpe_oos": 1.0})]
        a = reg.register(BuyAndHold({"ticker": "AAPL.US"}), reports=reports)
        b = reg.register(BuyAndHold({"ticker": "MSFT.US"}), reports=reports)
        seed_status(reg, a, "active")
    finally:
        state.close()
    return a, b


def test_registry_list_prints_both(runner, cli_env):
    runner.invoke(app, ["db", "init"])
    a, b = _seed_registry(cli_env)
    result = runner.invoke(app, ["registry", "list"])
    assert result.exit_code == 0, result.output
    assert a in result.output
    assert b in result.output
    assert "active" in result.output
    assert "shadow" in result.output


def test_registry_list_status_filter(runner, cli_env):
    runner.invoke(app, ["db", "init"])
    a, b = _seed_registry(cli_env)
    result = runner.invoke(app, ["registry", "list", "--status", "active"])
    assert result.exit_code == 0, result.output
    assert a in result.output
    assert b not in result.output


def test_registry_show_prints_params_and_reports(runner, cli_env):
    runner.invoke(app, ["db", "init"])
    a, _ = _seed_registry(cli_env)
    result = runner.invoke(app, ["registry", "show", a])
    assert result.exit_code == 0, result.output
    assert "AAPL.US" in result.output
    assert "oos" in result.output


def test_registry_promote_and_retire_are_governed(runner, cli_env):
    """BL-24: promote needs a passing go-live check (or an override with a
    reason) and retire needs a reason. Without those the change is refused
    and the status stays put."""
    runner.invoke(app, ["db", "init"])
    _, b = _seed_registry(cli_env)
    r1 = runner.invoke(app, ["registry", "promote", b])
    assert r1.exit_code != 0
    r2 = runner.invoke(app, ["registry", "list", "--status", "shadow"])
    assert b in r2.output
    r3 = runner.invoke(app, ["registry", "retire", b])
    assert r3.exit_code != 0
    r4 = runner.invoke(app, ["registry", "list", "--status", "retired"])
    assert b not in r4.output


@pytest.mark.parametrize("command", ["promote", "retire"])
def test_registry_status_change_unknown_id_errors(runner, cli_env, command):
    runner.invoke(app, ["db", "init"])
    _seed_registry(cli_env)
    result = runner.invoke(app, ["registry", command, "no_such_strategy"])
    assert result.exit_code == 1, result.output
    assert "no strategy with id" in result.output
    assert "→" not in result.output


def test_registry_list_asset_class_filter(runner, cli_env):
    """``--asset-class`` filters to strategies whose
    ``applicable_asset_classes`` contains the requested class. The
    default BuyAndHold strategies are equity-only, so an --asset-class=crypto
    filter should produce an empty list."""
    runner.invoke(app, ["db", "init"])
    a, b = _seed_registry(cli_env)
    result = runner.invoke(app, ["registry", "list", "--asset-class", "equity"])
    assert result.exit_code == 0, result.output
    assert a in result.output
    assert b in result.output

    crypto_only = runner.invoke(app, ["registry", "list", "--asset-class", "crypto"])
    assert crypto_only.exit_code == 0, crypto_only.output
    assert a not in crypto_only.output
    assert b not in crypto_only.output

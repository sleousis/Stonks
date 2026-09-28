"""CLI tests for `stonks starter`: the starter set On trial, once, with the
trading universe set when none is configured."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from stonks.cli import _settings, app
from stonks.store.state import SqliteState

CONFIG = """
[lake]
path = "data/lake.duckdb"

[state]
path = "data/state.sqlite"

[registry]
artifacts_dir = "data/artifacts"
""".strip()


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("STONKS_DATA_DIR", raising=False)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "default.toml").write_text(CONFIG)
    (tmp_path / "data").mkdir()
    state = SqliteState(tmp_path / "data" / "state.sqlite")
    state.migrate()
    state.close()
    return tmp_path


def test_install_puts_the_starters_on_trial_once(runner, workdir):
    first = runner.invoke(app, ["starter", "install"])
    assert first.exit_code == 0, first.output
    assert "on trial: starter_momentum" in first.output and "trading universe" in first.output
    assert _settings().production.universe[0] == "SPY.US"
    state = SqliteState(workdir / "data" / "state.sqlite")
    try:
        statuses = {r["status"] for r in state.sql("SELECT status FROM strategies")}
    finally:
        state.close()
    assert statuses == {"shadow"}
    again = runner.invoke(app, ["starter", "install"])
    assert again.exit_code == 0 and "already registered" in again.output
    listed = runner.invoke(app, ["starter", "list"])
    assert "starter_trend: shadow" in listed.output

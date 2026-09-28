"""CLI tests for `stonks algos` and `stonks plan` (roadmap 23.16)."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest
from typer.testing import CliRunner

from stonks.accounts import PortfolioRepository, Role, Scope, UserRepository
from stonks.cli import app
from stonks.store.lake import DuckDBLake
from stonks.store.state import SqliteState
from tests.integration.test_cli_tickets import CONFIG


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("STONKS_DATA_DIR", raising=False)
    monkeypatch.setenv("COLUMNS", "400")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "default.toml").write_text(CONFIG)
    (tmp_path / "data").mkdir()
    state = SqliteState(tmp_path / "data" / "state.sqlite")
    state.migrate()
    user = UserRepository(state).create(
        display_name="a", role=Role.TRADER, actor="t", email="a@x.io"
    )
    pf = PortfolioRepository(state).create(Scope.for_user(user), name="G", initial_cash=10_000.0).id
    state.close()
    lake = DuckDBLake(tmp_path / "data" / "lake.duckdb")
    lake.migrate()
    lake.upsert_prices(
        pd.DataFrame(
            {
                "ticker": ["UP.US"],
                "date": [date(2026, 3, 20)],
                "open": [50.0],
                "high": [50.0],
                "low": [50.0],
                "close": [50.0],
                "adj_close": [50.0],
                "volume": [1e6],
            }
        )
    )
    lake.close()
    return tmp_path, pf


def test_algos_list_set_show_and_clear(runner, workdir):
    _, pf = workdir
    listed = runner.invoke(app, ["algos", "list"])
    assert listed.exit_code == 0, listed.output
    assert "adaptive" in listed.output and "vwap" in listed.output
    done = runner.invoke(
        app, ["algos", "set", "twap", "--portfolio", pf, "--params", '{"slices": 4}']
    )
    assert done.exit_code == 0, done.output
    shown = runner.invoke(app, ["algos", "show", "--portfolio", pf])
    assert "twap" in shown.output and '"slices": 4' in shown.output
    bad = runner.invoke(app, ["algos", "set", "twap", "--portfolio", pf, "--params", "{nope"])
    assert bad.exit_code != 0
    assert "no parent orders" in runner.invoke(app, ["algos", "parents", "--portfolio", pf]).output
    assert "cleared" in runner.invoke(app, ["algos", "clear", "--portfolio", pf]).output
    assert "plain orders" in runner.invoke(app, ["algos", "show", "--portfolio", pf]).output


def test_plan_preview_and_confirm(runner, workdir):
    tmp, pf = workdir
    preview = runner.invoke(app, ["plan", "preview", "--portfolio", pf, "--targets", "UP.US=0.5"])
    assert preview.exit_code == 0, preview.output
    assert "buy 100" in preview.output and "turnover" in preview.output
    with SqliteState(tmp / "data" / "state.sqlite") as state:
        assert state.sql("SELECT COUNT(*) FROM order_tickets")[0][0] == 0
    confirmed = runner.invoke(
        app,
        ["plan", "confirm", "--portfolio", pf, "--targets", "UP.US=0.5", "--reason", "rebalance",
         "--yes"],
    )  # fmt: skip
    assert confirmed.exit_code == 0, confirmed.output
    assert "1 ticket(s) written" in confirmed.output
    with SqliteState(tmp / "data" / "state.sqlite") as state:
        [row] = state.sql("SELECT status, hold FROM order_tickets")
    assert tuple(row) == ("awaiting_approval", "approve_mode")


def test_plan_needs_one_source(runner, workdir):
    _, pf = workdir
    both = runner.invoke(app, ["plan", "preview", "--portfolio", pf])
    assert both.exit_code != 0
    bad = runner.invoke(app, ["plan", "preview", "--portfolio", pf, "--targets", "UP.US"])
    assert bad.exit_code != 0

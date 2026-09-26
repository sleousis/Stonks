"""CLI tests for `stonks tca` (BL-32): summary, journal, order, notes and
the benchmark refresh."""

from __future__ import annotations

import pandas as pd
import pytest
from typer.testing import CliRunner

from stonks.cli import app
from stonks.store.lake import DuckDBLake
from stonks.store.state import SqliteState

CONFIG = """
[lake]
path = "data/lake.duckdb"

[state]
path = "data/state.sqlite"

[registry]
artifacts_dir = "data/artifacts"
""".strip()

CID = "2026-03-20:mom:UP.US:buy"


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("STONKS_DATA_DIR", raising=False)
    monkeypatch.setenv("COLUMNS", "240")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "default.toml").write_text(CONFIG)
    (tmp_path / "data").mkdir()
    state = SqliteState(tmp_path / "data" / "state.sqlite")
    state.migrate()
    state.execute(
        "INSERT INTO orders (client_id, strategy_id, ticker, side, quantity, order_type, status,"
        " created_at, updated_at, portfolio_id, decision_price, decided_at,"
        " decision_context_json, expected_cost_bps)"
        " VALUES (?, NULL, 'UP.US', 'buy', 10, 'market', 'filled', ?, ?, 'pf_default', 100.0,"
        ' ?, \'{"trigger": "signal", "score": 0.02}\', 20.0)',
        [CID, *["2026-03-20T00:00:00+00:00"] * 3],
    )
    state.execute(
        "INSERT INTO fills (order_client_id, ticker, quantity, price, fee, filled_at,"
        " arrival_price) VALUES (?, 'UP.US', 10, 100.5, 0, ?, 100.0)",
        [CID, "2026-03-20T00:00:00+00:00"],
    )
    state.close()
    lake = DuckDBLake(tmp_path / "data" / "lake.duckdb")
    lake.migrate()
    lake.upsert_prices(
        pd.DataFrame(
            [
                {
                    "ticker": "UP.US",
                    "date": pd.Timestamp("2026-03-23").date(),
                    "open": 101.0,
                    "high": 103.0,
                    "low": 100.0,
                    "close": 102.0,
                    "adj_close": 102.0,
                    "volume": 1000,
                }
            ]
        )
    )
    lake.close()
    return tmp_path


def _rows(workdir, sql: str) -> list:
    state = SqliteState(workdir / "data" / "state.sqlite")
    try:
        return state.sql(sql)
    finally:
        state.close()


def test_summary_prints_the_shortfall(runner, workdir):
    result = runner.invoke(app, ["tca", "summary", "--by", "ticker"])
    assert result.exit_code == 0, result.output
    assert "UP.US" in result.output
    assert "50.0" in result.output  # 50 bps impact
    bad = runner.invoke(app, ["tca", "summary", "--by", "colour"])
    assert bad.exit_code != 0


def test_journal_and_order(runner, workdir):
    result = runner.invoke(app, ["tca", "journal"])
    assert result.exit_code == 0, result.output
    assert "signal" in result.output
    detail = runner.invoke(app, ["tca", "order", CID])
    assert detail.exit_code == 0, detail.output
    assert "is_bps" in detail.output
    missing = runner.invoke(app, ["tca", "order", "nope"])
    assert missing.exit_code != 0


def test_notes_are_added_and_edited(runner, workdir):
    added = runner.invoke(app, ["tca", "note", CID, "bought into strength"])
    assert added.exit_code == 0, added.output
    [note] = _rows(workdir, "SELECT id, author, note FROM journal_notes")
    assert note["author"] == "service:cli"
    edited = runner.invoke(app, ["tca", "edit-note", str(note["id"]), "bought the gap"])
    assert edited.exit_code == 0, edited.output
    assert _rows(workdir, "SELECT note FROM journal_notes")[0]["note"] == "bought the gap"
    journal = runner.invoke(app, ["tca", "journal"])
    assert "bought the gap" in journal.output


def test_refresh_fills_the_next_session(runner, workdir):
    result = runner.invoke(app, ["tca", "refresh"])
    assert result.exit_code == 0, result.output
    [row] = _rows(workdir, "SELECT benchmark_price, post_close_price FROM orders")
    assert (row["benchmark_price"], row["post_close_price"]) == (101.0, 102.0)

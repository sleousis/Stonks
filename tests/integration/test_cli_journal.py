"""CLI tests for `stonks journal` (roadmap 23.3): trades, review, calendar,
breakdown and playbooks."""

from __future__ import annotations

import json

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


@pytest.fixture
def runner():
    return CliRunner()


def _fill(state, cid, side, price, ts) -> int:
    state.execute(
        "INSERT INTO orders (client_id, ticker, side, quantity, order_type, status, created_at,"
        " updated_at, portfolio_id, origin) VALUES (?, 'UP.US', ?, 10, 'market', 'filled', ?, ?,"
        " 'pf_default', 'manual')",
        [cid, side, ts, ts],
    )
    cur = state.execute(
        "INSERT INTO fills (order_client_id, ticker, quantity, price, fee, filled_at, portfolio_id)"
        " VALUES (?, 'UP.US', 10, ?, 0, ?, 'pf_default')",
        [cid, price, ts],
    )
    return int(cur.lastrowid or 0)


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
    trade = _fill(state, "m1", "buy", 100.0, "2026-03-20T15:00:00+00:00")
    _fill(state, "m2", "sell", 104.0, "2026-03-23T15:00:00+00:00")
    state.close()
    lake = DuckDBLake(tmp_path / "data" / "lake.duckdb")
    lake.migrate()
    lake.upsert_prices(
        pd.DataFrame(
            [
                {
                    "ticker": "UP.US",
                    "date": pd.Timestamp(d).date(),
                    "open": 101.0,
                    "high": 106.0,
                    "low": 98.0,
                    "close": 102.0,
                    "adj_close": 102.0,
                    "volume": 1000,
                }
                for d in ("2026-03-20", "2026-03-23")
            ]
        )
    )
    lake.close()
    return tmp_path, trade


def test_trades_calendar_and_breakdown(runner, workdir):
    _, trade = workdir
    result = runner.invoke(app, ["journal", "trades", "--json"])
    assert result.exit_code == 0, result.output
    [leg] = json.loads(result.output)["items"]
    assert leg["trade_id"] == trade and leg["pnl"] == pytest.approx(40.0)
    assert leg["exit_efficiency"] == pytest.approx(0.75)
    table = runner.invoke(app, ["journal", "trades"])
    assert "UP.US" in table.output
    cal = runner.invoke(app, ["journal", "calendar", "--by", "week"])
    assert cal.exit_code == 0, cal.output
    assert "2026-W13" in cal.output
    assert runner.invoke(app, ["journal", "calendar", "--by", "year"]).exit_code != 0
    bad = runner.invoke(app, ["journal", "breakdown", "--by", "colour"])
    assert bad.exit_code != 0


def test_playbook_and_review(runner, workdir):
    _, trade = workdir
    added = runner.invoke(app, ["journal", "playbook-add", "Breakout", "--rules", "buy highs"])
    assert added.exit_code == 0, added.output
    pid = added.output.split()[1]
    listed = runner.invoke(app, ["journal", "playbooks"])
    assert "Breakout" in listed.output
    reviewed = runner.invoke(
        app,
        [
            "journal",
            "review",
            str(trade),
            "--tag",
            "gap",
            "--mistake",
            "late",
            "--playbook",
            pid,
            "--broke",
        ],
    )
    assert reviewed.exit_code == 0, reviewed.output
    shown = json.loads(runner.invoke(app, ["journal", "show", str(trade)]).output)
    assert (shown["tags"], shown["followed_plan"]) == (["gap"], False)
    by_plan = runner.invoke(app, ["journal", "breakdown", "--by", "plan", "--json"])
    assert json.loads(by_plan.output)["groups"][0]["key"] == "broke"
    labels = runner.invoke(app, ["journal", "labels"])
    assert "gap" in labels.output and "late" in labels.output
    edited = runner.invoke(app, ["journal", "playbook-edit", pid, "--archive"])
    assert "archived" in edited.output
    missing = runner.invoke(app, ["journal", "show", "99999"])
    assert missing.exit_code != 0

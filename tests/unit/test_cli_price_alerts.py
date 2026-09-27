"""``stonks price-alerts``: create, list, run, events and delete."""

from __future__ import annotations

import pandas as pd
from typer.testing import CliRunner

from stonks.accounts import Role, UserRepository
from stonks.cli import app
from stonks.store.lake import DuckDBLake
from stonks.store.state import SqliteState


def test_create_list_run_events_delete(tmp_path, monkeypatch):
    monkeypatch.setenv("STONKS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COLUMNS", "300")
    lake = DuckDBLake(tmp_path / "lake.duckdb")
    lake.migrate()
    lake.upsert_prices(
        pd.DataFrame(
            [
                {
                    "ticker": "UP.US",
                    "date": pd.Timestamp(d).date(),
                    "open": c,
                    "high": c,
                    "low": c,
                    "close": c,
                    "adj_close": c,
                    "volume": 1,
                }
                for d, c in (("2026-03-31", 9.0), ("2026-04-01", 11.0))
            ]
        )
    )
    lake.close()
    with SqliteState(tmp_path / "state.sqlite") as state:
        state.migrate()
        UserRepository(state).create(
            display_name="a", role=Role.TRADER, actor="t", email="a@example.com"
        )
    runner = CliRunner()
    who = ["--user", "a@example.com"]
    made = runner.invoke(
        app,
        [
            "price-alerts",
            "create",
            "--condition",
            "crosses_above",
            "--ticker",
            "UP.US",
            "--level",
            "10",
            *who,
        ],
    )
    assert made.exit_code == 0, made.output
    rid = made.output.split("created ")[1].split(":")[0]
    assert rid in runner.invoke(app, ["price-alerts", "list", *who]).output
    ran = runner.invoke(app, ["price-alerts", "run", "--as-of", "2026-04-01"])
    assert ran.exit_code == 0 and "1 fired" in ran.output
    assert "crossed above 10" in runner.invoke(app, ["price-alerts", "events", *who]).output
    bad = runner.invoke(
        app, ["price-alerts", "create", "--condition", "moves_pct", "--ticker", "X", *who]
    )
    assert bad.exit_code != 0
    assert runner.invoke(app, ["price-alerts", "delete", rid, *who]).exit_code == 0
    assert "no price alerts" in runner.invoke(app, ["price-alerts", "list", *who]).output

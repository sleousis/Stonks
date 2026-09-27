"""``stonks orders``: place, preview, list and cancel manual orders."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd
from typer.testing import CliRunner

from stonks.accounts import PortfolioRepository, Role, Scope, UserRepository
from stonks.cli import app
from stonks.store.lake import DuckDBLake
from stonks.store.state import SqliteState


def _seed(tmp_path) -> str:
    lake = DuckDBLake(tmp_path / "lake.duckdb")
    lake.migrate()
    today = datetime.now(UTC).date()
    days = pd.bdate_range(today - timedelta(days=14), today - timedelta(days=1))
    lake.upsert_prices(
        pd.DataFrame(
            [
                {
                    "ticker": "UP.US",
                    "date": d.date(),
                    "open": 20.0,
                    "high": 20.0,
                    "low": 20.0,
                    "close": 20.0,
                    "adj_close": 20.0,
                    "volume": 1_000,
                }
                for d in days
            ]
        )
    )
    lake.close()
    with SqliteState(tmp_path / "state.sqlite") as state:
        state.migrate()
        user = UserRepository(state).create(
            display_name="a", role=Role.TRADER, actor="t", email="a@example.com"
        )
        return (
            PortfolioRepository(state)
            .create(Scope.for_user(user), name="Mine", initial_cash=1_000.0)
            .id
        )


def test_place_preview_list_and_cancel(tmp_path, monkeypatch):
    monkeypatch.setenv("STONKS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COLUMNS", "400")
    pid = _seed(tmp_path)
    runner = CliRunner()
    base = ["--ticker", "UP.US", "--side", "buy", "--quantity", "3", "--user", "a@example.com"]

    shown = runner.invoke(app, ["orders", "preview", *base])
    assert shown.exit_code == 0, shown.output
    assert "preview" in shown.output

    placed = runner.invoke(
        app, ["orders", "place", *base, "--reason", "from the shell", "--client-id", "k1"]
    )
    assert placed.exit_code == 0, placed.output
    assert "filled" in placed.output and f"manual:{pid}:k1" in placed.output

    listed = runner.invoke(app, ["orders", "list", "--manual", "--user", "a@example.com"])
    assert listed.exit_code == 0 and "manual" in listed.output and "shell" in listed.output

    late = runner.invoke(
        app,
        ["orders", "cancel", f"manual:{pid}:k1", "--reason", "too late", "--user", "a@example.com"],
    )
    assert late.exit_code != 0 and "working order" in late.output

    refused = runner.invoke(app, ["orders", "place", *base[:-2], "--reason", "no book named"])
    assert refused.exit_code != 0 and "portfolio" in refused.output

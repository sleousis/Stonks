"""CLI tests for `stonks tick`.

Seeds a fresh lake with canned prices and a registered+active BuyAndHold
strategy, then runs the CLI tick end-to-end.
"""

from __future__ import annotations

import pandas as pd
import pytest
from typer.testing import CliRunner

from stonks.cli import app
from stonks.core.protocols import SurvivalReport
from stonks.registry.store import StrategyRegistry
from stonks.store.lake import DuckDBLake
from stonks.store.state import SqliteState
from stonks.strategies.examples.buy_and_hold import BuyAndHold


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def seeded(tmp_path, monkeypatch):
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

[production]
universe = ["UP.US"]
threshold = 0.0
initial_cash = 10000.0

[sources.eodhd]
base_url = "https://example.test/api"
""".strip()
    )
    monkeypatch.setenv("EODHD_API_KEY", "test-key")

    # seed lake + state
    (tmp_path / "data").mkdir()
    lake = DuckDBLake(tmp_path / "data" / "lake.duckdb")
    lake.migrate()
    dates = pd.bdate_range(start="2026-03-01", periods=20)
    closes = [100.0 + i * 2.0 for i in range(len(dates))]
    lake.upsert_prices(
        pd.DataFrame(
            [
                {
                    "ticker": "UP.US",
                    "date": d.date(),
                    "open": c,
                    "high": c + 1,
                    "low": c - 1,
                    "close": c,
                    "adj_close": c,
                    "volume": 1_000_000,
                }
                for d, c in zip(dates, closes, strict=False)
            ]
        )
    )
    lake.close()

    state = SqliteState(tmp_path / "data" / "state.sqlite")
    state.migrate()
    registry = StrategyRegistry(state=state, artifacts_dir=tmp_path / "data" / "artifacts")
    sid = registry.register(
        BuyAndHold({"ticker": "UP.US", "allocation": 1.0}),
        reports=[SurvivalReport(test_id="oos", passed=True, metrics={"sharpe_oos": 1.0})],
    )
    registry.set_status(sid, "active")
    state.close()

    return tmp_path, sid


def test_tick_dry_run_smoke(runner, seeded):
    _, _ = seeded
    result = runner.invoke(app, ["tick", "--dry-run", "--as-of", "2026-03-20"])
    assert result.exit_code == 0, result.output
    assert "dry-run" in result.output


def test_tick_full_run_smoke(runner, seeded):
    tmp_path, _ = seeded
    result = runner.invoke(app, ["tick", "--as-of", "2026-03-20"])
    assert result.exit_code == 0, result.output

    # verify state tables populated
    state = SqliteState(tmp_path / "data" / "state.sqlite")
    try:
        assert state.count_rows("tick_runs") == 1
        assert state.count_rows("orders") >= 1
        assert state.count_rows("fills") >= 1
        assert state.count_rows("portfolio_snapshots") == 1
    finally:
        state.close()


def test_tick_with_tickers_override(runner, seeded):
    result = runner.invoke(
        app, ["tick", "--dry-run", "--as-of", "2026-03-20", "--tickers", "UP.US"]
    )
    assert result.exit_code == 0, result.output

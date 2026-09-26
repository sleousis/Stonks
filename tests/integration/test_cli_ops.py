"""CLI tests for `stonks health` and `stonks pnl` (roadmap 2.5)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

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

[production]
universe = ["UP.US"]
""".strip()


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("STONKS_DATA_DIR", raising=False)
    monkeypatch.delenv("STONKS_NOTIFY_WEBHOOK_URL", raising=False)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "default.toml").write_text(CONFIG)
    (tmp_path / "data").mkdir()
    lake = DuckDBLake(tmp_path / "data" / "lake.duckdb")
    lake.migrate()
    today = datetime.now(UTC).date()
    lake.upsert_prices(
        pd.DataFrame(
            [
                {
                    "ticker": "UP.US",
                    "date": today - timedelta(days=i),
                    "open": 1.0,
                    "high": 1.0,
                    "low": 1.0,
                    "close": 1.0,
                    "adj_close": 1.0,
                    "volume": 1,
                }
                for i in range(5)
            ]
        )
    )
    lake.close()
    state = SqliteState(tmp_path / "data" / "state.sqlite")
    state.migrate()
    state.close()
    return tmp_path


def test_health_ok_exits_zero(runner, workdir):
    result = runner.invoke(app, ["health"])
    assert result.exit_code == 0, result.output
    assert "healthy" in result.output


def test_health_stale_ticker_exits_one(runner, workdir):
    result = runner.invoke(app, ["health", "--tickers", "UP.US,NOPE.US"])
    assert result.exit_code == 1, result.output
    assert "NOPE.US" in result.output
    assert "UNHEALTHY" in result.output


def test_health_notify_posts_webhook_only_when_asked(runner, workdir, monkeypatch):
    import stonks.notify.webhook as webhook_mod

    posts: list[dict] = []

    class FakeResponse:
        def raise_for_status(self):
            return None

    class FakeSession:
        def post(self, url, json=None, timeout=None, headers=None):
            posts.append(json)
            return FakeResponse()

    monkeypatch.setattr(webhook_mod.requests, "Session", FakeSession)
    monkeypatch.setenv("STONKS_NOTIFY_WEBHOOK_URL", "https://hooks.example.test/tok")
    cfg = workdir / "config" / "default.toml"
    cfg.write_text(CONFIG + '\n\n[notify]\nbackends = ["webhook"]\n')

    runner.invoke(app, ["health", "--tickers", "NOPE.US"])
    assert posts == []
    result = runner.invoke(app, ["health", "--tickers", "NOPE.US", "--notify"])
    assert result.exit_code == 1
    assert len(posts) == 1
    assert posts[0]["level"] == "error"


def _snap(workdir, tick_id, taken_at, value):
    state = SqliteState(workdir / "data" / "state.sqlite")
    try:
        state.execute(
            "INSERT INTO tick_runs (id, started_at, status) VALUES (?, ?, 'ok')",
            [tick_id, taken_at],
        )
        state.execute(
            "INSERT INTO portfolio_snapshots (tick_id, taken_at, cash, positions_json, "
            "total_value) VALUES (?, ?, ?, '{}', ?)",
            [tick_id, taken_at, value, value],
        )
    finally:
        state.close()


def test_pnl_prints_daily_table(runner, workdir):
    _snap(workdir, "t1", "2026-01-01T15:00:00+00:00", 10_000.0)
    _snap(workdir, "t2", "2026-01-02T15:00:00+00:00", 10_500.0)
    _snap(workdir, "t3", "2026-01-05T15:00:00+00:00", 9_975.0)
    result = runner.invoke(app, ["pnl"])
    assert result.exit_code == 0, result.output
    assert "2026-01-01" in result.output
    assert "2026-01-05" in result.output
    assert "+5.00%" in result.output  # day 2 return
    assert "-5.00%" in result.output  # day 3 drawdown / daily return


def test_pnl_since_filters_rows(runner, workdir):
    _snap(workdir, "t1", "2026-01-01T15:00:00+00:00", 10_000.0)
    _snap(workdir, "t2", "2026-01-02T15:00:00+00:00", 10_500.0)
    result = runner.invoke(app, ["pnl", "--since", "2026-01-02"])
    assert result.exit_code == 0, result.output
    assert "2026-01-01" not in result.output
    assert "2026-01-02" in result.output


def test_pnl_without_snapshots(runner, workdir):
    result = runner.invoke(app, ["pnl"])
    assert result.exit_code == 0, result.output
    assert "no portfolio snapshots" in result.output


def test_pnl_rejects_bad_since(runner, workdir):
    result = runner.invoke(app, ["pnl", "--since", "yesterday"])
    assert result.exit_code != 0

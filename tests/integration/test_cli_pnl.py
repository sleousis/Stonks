"""`stonks pnl` shows the gap between rows and groups by the tick's as_of."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from stonks.cli import app
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
def workdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("STONKS_DATA_DIR", raising=False)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "default.toml").write_text(CONFIG)
    state = SqliteState(tmp_path / "data" / "state.sqlite")
    state.migrate()
    state.close()
    return tmp_path


def _snap(workdir, tick_id, as_of, taken_at, value):
    state = SqliteState(workdir / "data" / "state.sqlite")
    try:
        state.execute(
            "INSERT INTO tick_runs (id, started_at, status) VALUES (?, ?, 'ok')",
            [tick_id, taken_at],
        )
        state.execute(
            "INSERT INTO portfolio_snapshots (tick_id, as_of, taken_at, cash, positions_json,"
            " total_value) VALUES (?, ?, ?, ?, '{}', ?)",
            [tick_id, as_of, taken_at, value, value],
        )
    finally:
        state.close()


def test_pnl_shows_days_column_and_blanks_daily_return_across_a_gap(workdir):
    _snap(workdir, "t1", "2026-01-02", "2026-01-03T00:30:00+00:00", 10_000.0)
    _snap(workdir, "t2", "2026-01-05", "2026-01-05T22:45:00+00:00", 10_500.0)
    _snap(workdir, "t3", "2026-01-20", "2026-01-20T22:45:00+00:00", 11_000.0)
    result = CliRunner().invoke(app, ["pnl"])
    assert result.exit_code == 0, result.output
    assert "days" in result.output
    assert "2026-01-02" in result.output  # as_of, not the UTC day it ran
    assert "2026-01-03" not in result.output
    assert "+5.00%" in result.output  # Friday -> Monday counts as daily
    assert "+4.76%" not in result.output  # 11000/10500 - 1 spans a 15-day gap

"""``stonks ingest backfill``: refuses the free plan, needs migrated stores,
and reports progress from the state database. Hermetic: no key, no network."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from typer.testing import CliRunner

from stonks.cli import app
from stonks.ingest.backfill.state import BackfillState, UnitSpec
from stonks.store.state import SqliteState


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "default.toml").write_text(
        '[lake]\npath = "data/lake.duckdb"\n\n[state]\npath = "data/state.sqlite"\n'
    )
    monkeypatch.delenv("EODHD_API_KEY", raising=False)
    monkeypatch.delenv("STONKS_EODHD_PLAN", raising=False)
    import dotenv

    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: False)
    return tmp_path


def test_run_refuses_the_free_plan(runner, cli_env):
    result = runner.invoke(app, ["ingest", "backfill", "run", "--until-budget"])
    assert result.exit_code == 2
    assert "STONKS_EODHD_PLAN" in result.output


def test_run_needs_a_key_on_the_paid_plan(runner, cli_env, monkeypatch):
    monkeypatch.setenv("STONKS_EODHD_PLAN", "all_in_one")
    result = runner.invoke(app, ["ingest", "backfill", "run", "--until-budget"])
    assert result.exit_code == 2
    assert "EODHD_API_KEY" in result.output


def test_status_asks_for_db_init_first(runner, cli_env):
    result = runner.invoke(app, ["ingest", "backfill", "status"])
    assert result.exit_code == 2
    assert "stonks db init" in result.output


def test_status_and_retry_read_the_state(runner, cli_env):
    with SqliteState(cli_env / "data" / "state.sqlite") as sqlite:
        sqlite.migrate()
        state = BackfillState(sqlite)
        state.add_units("daily", [UnitSpec("A.US", "US"), UnitSpec("B.US", "US")])
        unit = state.next_units("daily", 1)[0]
        state.mark_done(unit, calls=1, rows=5)
        other = state.next_units("daily", 1)[0]
        state.mark_failure(other, error="boom", calls=1, max_attempts=1, backoff=timedelta(0))
    result = runner.invoke(app, ["ingest", "backfill", "status", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    daily = next(p for p in payload["phases"] if p["phase"] == "daily")
    assert (daily["done"], daily["failed"], daily["remaining"]) == (1, 1, 0)
    assert payload["daily_budget"] == 95_000
    table = runner.invoke(app, ["ingest", "backfill", "status"])
    assert table.exit_code == 0
    assert "not listed yet" in table.output
    retried = runner.invoke(app, ["ingest", "backfill", "retry", "--phases", "daily"])
    assert retried.exit_code == 0
    assert "1 failed units" in retried.output

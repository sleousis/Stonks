"""`stonks report --backtest`: a tear sheet (strategy vs benchmark) for a
backtest job, a registered strategy or a catalog strategy (BL-22)."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from stonks.app.jobs import JobStore
from stonks.cli import app
from stonks.registry.store import StrategyRegistry
from stonks.store.state import SqliteState
from stonks.strategies.examples.buy_and_hold import BuyAndHold

WINDOW = ["--start", "2025-10-01", "--end", "2026-04-01"]


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def env(tmp_path, monkeypatch, lake_trending):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    lake_path = (tmp_path / "lake.duckdb").as_posix()
    (tmp_path / "config" / "default.toml").write_text(
        f"""
[lake]
path = "{lake_path}"

[state]
path = "state.sqlite"

[registry]
artifacts_dir = "artifacts"
""".strip()
    )
    with SqliteState(tmp_path / "state.sqlite") as state:
        state.migrate()
    return tmp_path


def _report(runner, *args):
    return runner.invoke(app, ["report", *args], catch_exceptions=False)


def test_catalog_strategy_tear_sheet(runner, env):
    out = env / "ts.html"
    r = _report(runner, "--backtest", "buy_and_hold", "--params", '{"ticker": "UP.US"}',
                "--tickers", "UP.US,DOWN.US", *WINDOW, "--out", str(out))  # fmt: skip
    assert r.exit_code == 0, r.output
    html = out.read_text(encoding="utf-8")
    assert html.startswith("<!doctype html>")
    assert "Tear sheet" in html and "Strategy vs benchmark" in html
    assert "EW CAGR" in html  # "auto" falls back to the equal-weight universe


def test_registered_strategy_tear_sheet_with_a_named_benchmark(runner, env):
    with SqliteState(env / "state.sqlite") as state:
        registry = StrategyRegistry(state=state, artifacts_dir=env / "artifacts")
        sid = registry.register(BuyAndHold({"ticker": "UP.US"}), [], strategy_id="bah_up")
    out = env / "ts.html"
    r = _report(runner, "--backtest", sid, "--tickers", "UP.US", *WINDOW,
                "--benchmark", "DOWN.US", "--out", str(out))  # fmt: skip
    assert r.exit_code == 0, r.output
    html = out.read_text(encoding="utf-8")
    assert "bah_up" in html and "DOWN.US CAGR" in html


def test_backtest_job_tear_sheet_reruns_the_job_request(runner, env):
    params = {
        "strategy": {
            "class_path": "stonks.strategies.examples.buy_and_hold:BuyAndHold",
            "params": {"ticker": "UP.US"},
        },
        "universe": ["UP.US"],
        "start": "2025-10-01",
        "end": "2026-04-01",
    }
    job = JobStore(env / "state.sqlite").create("backtest", params)
    out = env / "ts.html"
    r = _report(runner, "--backtest", job.id, "--out", str(out))
    assert r.exit_code == 0, r.output
    assert "Strategy vs benchmark" in out.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("args", "needle"),
    [
        (["--backtest", "buy_and_hold", "--tickers", "UP.US"], "--start"),
        (["--backtest", "nope", *WINDOW, "--tickers", "UP.US"], "nope"),
        (["--backtest", "buy_and_hold", *WINDOW], "--tickers"),
    ],
)
def test_bad_backtest_target_is_a_usage_error(runner, env, args, needle):
    r = runner.invoke(app, ["report", *args])
    assert r.exit_code == 2, r.output
    assert needle in r.output


def test_a_non_backtest_job_is_rejected(runner, env):
    job = JobStore(env / "state.sqlite").create("lab_run", {"x": json.dumps(1)})
    r = runner.invoke(app, ["report", "--backtest", job.id])
    assert r.exit_code == 2, r.output
    assert "lab_run" in r.output


def test_a_configured_universe_id_keeps_the_membership_gate(env, lake_trending):
    """BE-07: a tear sheet over ``[production].universe = "<id>"`` carries
    the id, so the backtest trades each name only while it is a member."""
    from datetime import date

    import pandas as pd

    from stonks.app.context import AppContext
    from stonks.app.tearsheets import TearSheetWindow, tear_sheet_request
    from stonks.config import load_settings

    lake_trending.upsert_universe_membership(
        pd.DataFrame(
            [
                {"universe_id": "idx", "ticker": "UP.US", "start_date": date(2025, 10, 1)},
                {"universe_id": "idx", "ticker": "DOWN.US", "start_date": date(2026, 1, 5)},
            ]
        )
    )
    settings = load_settings()
    settings.production.universe = "idx"
    window = TearSheetWindow(start=date(2025, 10, 1), end=date(2026, 4, 1))
    request = tear_sheet_request(AppContext(settings), "buy_and_hold", window)
    assert request.universe_id == "idx"
    assert sorted(request.universe) == ["DOWN.US", "UP.US"]

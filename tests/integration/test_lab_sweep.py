"""`stonks lab sweep`: plan, parallel execution and the CSV / JSON summary."""

from __future__ import annotations

import csv
import dataclasses
import json
from datetime import date

import pytest
from typer.testing import CliRunner

from stonks.app.lab import LabRunRequest
from stonks.app.strategies import StrategyRef
from stonks.app.sweep import SweepRow, SweepTask, plan_sweep, row_record, run_sweep
from stonks.cli import app
from stonks.config import LakeConfig, RegistryConfig, Settings, StateConfig
from stonks.lab.catalog import strategy_catalog
from stonks.lab.parallel import ParallelSettings
from stonks.store.state import SqliteState

BASKET = ["UP.US", "DOWN.US"]
BAH = "stonks.strategies.examples.buy_and_hold:BuyAndHold"
MOMENTUM = "stonks.strategies.examples.momentum:Momentum"


def test_plan_runs_single_ticker_strategies_per_ticker():
    tasks = plan_sweep(BASKET, ["momentum", "buy_and_hold"])
    assert tasks == [
        SweepTask("buy_and_hold", BAH, "UP.US"),
        SweepTask("buy_and_hold", BAH, "DOWN.US"),
        SweepTask("momentum", MOMENTUM, None),
    ]


def test_plan_defaults_to_every_non_wrapper_strategy_and_honours_exclude():
    tasks = plan_sweep(["A.US"], exclude=["momentum"])
    ids = {t.strategy_id for t in tasks}
    assert "momentum" not in ids
    assert "macro_regime_filter" not in ids  # wrappers need an inner strategy
    assert ids == set(strategy_catalog()) - {
        "momentum",
        "macro_regime_filter",
        "feature_regime_filter",
        "last_trade_filter",
    }


def test_plan_rejects_unknown_strategies():
    with pytest.raises(ValueError, match="unknown strategy"):
        plan_sweep(BASKET, ["nope"])


def test_row_record_flattens_survival_metrics():
    row = SweepRow(
        strategy="momentum",
        ticker=None,
        verdict="pass",
        best_score=1.2,
        best_params={"lookback_days": 5},
        n_trials=3,
        survival={"oos": {"passed": True, "metrics": {"sharpe_oos": 0.9}}},
    )
    record = row_record(row)
    assert record["ticker"] == "*"
    assert json.loads(record["best_params"]) == {"lookback_days": 5}
    assert record["oos.passed"] is True and record["oos.sharpe_oos"] == 0.9


@pytest.fixture
def sweep_settings(tmp_path, lake_trending):
    return Settings(
        lake=LakeConfig(path=tmp_path / "lake.duckdb"),
        state=StateConfig(path=tmp_path / "state.sqlite"),
        registry=RegistryConfig(artifacts_dir=tmp_path / "artifacts"),
    )


def _request(**extra) -> LabRunRequest:
    return LabRunRequest(
        strategy=StrategyRef(class_path=MOMENTUM),
        universe=BASKET,
        start=date(2025, 10, 1),
        end=date(2026, 4, 1),
        budget=2,
        survival_tests=["oos"],
        **extra,
    )


def _comparable(rows):
    return [dataclasses.replace(r, run_id="") for r in rows]


def test_sweep_is_deterministic_across_worker_counts(sweep_settings, lake_trending):
    tasks = plan_sweep(BASKET, ["momentum", "buy_and_hold"])
    serial = run_sweep(
        sweep_settings,
        tasks,
        _request(),
        lake=lake_trending,
        parallel=ParallelSettings(max_workers=1),
    )
    parallel = run_sweep(
        sweep_settings,
        tasks,
        _request(),
        lake=lake_trending,
        parallel=ParallelSettings(max_workers=2),
    )
    assert [(r.strategy, r.ticker) for r in serial] == [
        ("buy_and_hold", "UP.US"),
        ("buy_and_hold", "DOWN.US"),
        ("momentum", None),
    ]
    assert all(r.verdict in ("pass", "fail") for r in serial), [r.error for r in serial]
    assert serial[0].best_params["ticker"] == "UP.US"
    assert _comparable(serial) == _comparable(parallel)
    # every run went through the trial ledger
    with SqliteState(sweep_settings.state.path) as state:
        runs = state.sql("SELECT COUNT(*) AS n FROM lab_runs")[0]["n"]
    assert runs == 6


def test_a_failing_task_becomes_an_error_row(sweep_settings, lake_trending):
    tasks = [SweepTask("momentum", "no.such.module:Nope", None)]
    (row,) = run_sweep(sweep_settings, tasks, _request(), lake=lake_trending)
    assert row.verdict == "error" and row.error


def test_sweep_refuses_to_register(sweep_settings, lake_trending):
    with pytest.raises(ValueError, match="never registers"):
        run_sweep(
            sweep_settings,
            plan_sweep(BASKET, ["momentum"]),
            _request(register_if_passes=True),
            lake=lake_trending,
        )


def test_cli_sweep_writes_csv_and_json(tmp_path, monkeypatch, lake_trending):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "default.toml").write_text(
        f'[lake]\npath = "{(tmp_path / "lake.duckdb").as_posix()}"\n'
        '[state]\npath = "data/state.sqlite"\n'
        '[registry]\nartifacts_dir = "data/artifacts"\n'
    )
    csv_path, json_path = tmp_path / "sweep.csv", tmp_path / "sweep.json"
    result = CliRunner().invoke(
        app,
        [
            "lab",
            "sweep",
            "--tickers",
            ",".join(BASKET),
            "--start",
            "2025-10-01",
            "--end",
            "2026-04-01",
            "--strategies",
            "momentum,buy_and_hold",
            "--budget",
            "2",
            "--tests",
            "oos",
            "--workers",
            "1",
            "--csv-out",
            str(csv_path),
            "--json-out",
            str(json_path),
        ],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    with open(csv_path, newline="", encoding="utf-8") as fh:
        records = list(csv.DictReader(fh))
    assert [(r["strategy"], r["ticker"]) for r in records] == [
        ("buy_and_hold", "UP.US"),
        ("buy_and_hold", "DOWN.US"),
        ("momentum", "*"),
    ]
    assert {"verdict", "best_score", "best_params", "oos.passed"} <= set(records[0])
    doc = json.loads(json_path.read_text())
    assert [d["strategy"] for d in doc] == ["buy_and_hold", "buy_and_hold", "momentum"]
    assert "pass" in result.output or "fail" in result.output


def test_cli_sweep_rejects_unknown_strategies(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(
        app,
        [
            "lab",
            "sweep",
            "--tickers",
            "A.US",
            "--start",
            "2025-01-01",
            "--end",
            "2025-06-01",
            "--strategies",
            "nope",
        ],
    )
    assert result.exit_code == 2
    assert "unknown strategy" in result.output

"""lab.parallel: an ordered process-pool map with per-worker state, plus
picklable handles that carry a strategy and a lake into worker processes."""

from __future__ import annotations

import os
import pickle

import numpy as np
import pandas as pd
import pytest

from stonks.core.interval import Interval
from stonks.lab.dataset import LabDataset
from stonks.lab.parallel import LakeHandle, StrategyHandle, default_max_workers, run_tasks
from stonks.store.lake import DuckDBLake
from stonks.strategies.examples.rsi_pca import RSIPCAStrategy

# ---- task functions (module level so worker processes can import them) ----


def _setup(payload):
    return {"base": payload, "pid": os.getpid()}


def _task(state, task):
    return state["base"] * task, state["pid"]


def _boom(state, task):
    raise RuntimeError(f"task {task} failed")


def _lake_task(handle: LakeHandle, ticker: str):
    lake = handle.lake
    n_bars = int(lake.sql("SELECT COUNT(*) AS n FROM bars")["n"].iloc[0])
    ids = sorted(lake.sql("SELECT id FROM instruments")["id"])
    return ticker, n_bars, ids


# ---- run_tasks --------------------------------------------------------------


def test_serial_run_calls_setup_once_in_process_and_keeps_order():
    out = run_tasks(_task, [3, 1, 2], setup=_setup, payload=10, max_workers=1)
    assert [v for v, _ in out] == [30, 10, 20]
    assert {pid for _, pid in out} == {os.getpid()}


def test_parallel_run_matches_serial_and_runs_in_workers():
    tasks = list(range(7))
    serial = run_tasks(_task, tasks, setup=_setup, payload=5, max_workers=1)
    parallel = run_tasks(_task, tasks, setup=_setup, payload=5, max_workers=2)
    assert [v for v, _ in parallel] == [v for v, _ in serial]
    assert os.getpid() not in {pid for _, pid in parallel}


def test_worker_errors_propagate():
    with pytest.raises(RuntimeError, match="failed"):
        run_tasks(_boom, [1, 2], max_workers=2)


def test_no_tasks_no_pool():
    assert run_tasks(_task, [], setup=_setup, payload=1, max_workers=4) == []


def test_max_workers_must_be_positive():
    with pytest.raises(ValueError):
        run_tasks(_task, [1], max_workers=0)


def test_default_max_workers_uses_env_override_else_cpu_count(monkeypatch):
    # ``default_max_workers`` is the function imported above; conftest only
    # replaces the module attribute (tests default to in-process runs)
    monkeypatch.setenv("STONKS_LAB_MAX_WORKERS", "3")
    assert default_max_workers() == 3
    monkeypatch.delenv("STONKS_LAB_MAX_WORKERS")
    assert default_max_workers() == (os.cpu_count() or 1)


# ---- handles ----------------------------------------------------------------


@pytest.fixture
def lake(tmp_path):
    rng = np.random.default_rng(5)
    dates = pd.bdate_range("2024-01-02", periods=300)
    close = np.exp(np.log(50.0) + np.cumsum(rng.normal(0.0003, 0.015, len(dates))))
    lake = DuckDBLake(tmp_path / "lake.duckdb")
    lake.migrate()
    for ticker in ("X.US", "OTHER.US"):
        lake.upsert_prices(
            pd.DataFrame(
                {
                    "ticker": ticker,
                    "date": [d.date() for d in dates],
                    "open": close,
                    "high": close * 1.01,
                    "low": close * 0.99,
                    "close": close,
                    "adj_close": close,
                    "volume": 1_000,
                }
            )
        )
    lake.con.execute(
        "INSERT INTO instruments (id, asset_class) VALUES ('X.US', 'equity'), ('OTHER.US', 'equity')"
    )
    yield lake, dates
    lake.close()


def test_strategy_handle_is_the_instance_in_process_and_a_faithful_copy_when_pickled(lake):
    lake, dates = lake
    strategy = RSIPCAStrategy({"ticker": "X.US", "n_components": 2, "long_quantile": 0.9})
    strategy.fit(
        LabDataset(
            lake=lake,
            universe=["X.US"],
            start=dates[0].date(),
            end=dates[-1].date(),
            interval=Interval.DAY_1,
        )
    )
    handle = StrategyHandle(strategy)
    assert handle.strategy is strategy

    copy = pickle.loads(pickle.dumps(handle)).strategy
    assert type(copy) is RSIPCAStrategy and copy is not strategy
    assert copy.params == strategy.params
    for d in dates[-30:]:
        assert copy.estimate_return("X.US", d.date(), lake) == strategy.estimate_return(
            "X.US", d.date(), lake
        )


def test_unfitted_strategy_handle_round_trips():
    strategy = RSIPCAStrategy({"ticker": "X.US"})
    copy = pickle.loads(pickle.dumps(StrategyHandle(strategy))).strategy
    assert copy.params == strategy.params and not copy.is_fitted


def test_lake_handle_carries_the_universe_tables_without_bars(lake):
    lake, _ = lake
    handle = LakeHandle(lake, ["X.US"])
    assert handle.lake is lake

    copy = pickle.loads(pickle.dumps(handle))
    try:
        assert copy.lake is not lake
        assert list(copy.lake.sql("SELECT id FROM instruments")["id"]) == ["X.US"]
        assert int(copy.lake.sql("SELECT COUNT(*) AS n FROM bars")["n"].iloc[0]) == 0
    finally:
        copy.close()


def test_lake_handle_opens_in_each_worker(lake):
    lake, _ = lake
    out = run_tasks(
        _lake_task,
        ["a", "b", "c"],
        setup=_identity,
        payload=LakeHandle(lake, ["X.US"]),
        max_workers=2,
    )
    assert out == [("a", 0, ["X.US"]), ("b", 0, ["X.US"]), ("c", 0, ["X.US"])]


def _identity(payload):
    return payload

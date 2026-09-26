"""Lab snapshots over a Parquet bar store (roadmap 10.4): workers read the
universe's bar partitions directly (hard-linked into the snapshot, so the
run's data cannot change under it) instead of an exported bars table, and
every result is bit-identical to the DuckDB-table store."""

from __future__ import annotations

import os

import duckdb
import pandas as pd
import pytest

from stonks.backtest.engine import BacktestConfig, Backtester
from stonks.backtest.simulated_broker import SimulatedBroker
from stonks.core.types import Portfolio
from stonks.lab.dataset import LabDataset
from stonks.lab.objectives import SharpeObjective
from stonks.lab.parallel import (
    DatasetSpec,
    LakeSnapshot,
    ParallelSettings,
    dataset_snapshot,
    run_tasks,
)
from stonks.lab.tuning.grid import GridTuner
from stonks.strategies.examples.momentum import Momentum
from tests.fixtures.parallel_lab import lake_dates, random_walk_lake

UNIVERSE = ["A.US", "B.US", "C.US"]


def _lake(tmp_path, backend: str):
    lake = random_walk_lake(tmp_path / backend / "lake.duckdb", UNIVERSE, periods=160)
    if backend == "parquet":
        lake.migrate_bars_to_parquet()
    return lake


@pytest.fixture
def parquet_lake(tmp_path):
    lake = _lake(tmp_path, "parquet")
    yield lake
    lake.close()


def _worker_task(dataset, ticker):
    lake = dataset.lake
    n = int(lake.sql("SELECT COUNT(*) AS n FROM bars WHERE ticker = ?", [ticker])["n"].iloc[0])
    try:
        lake.con.execute("DELETE FROM bars")
        wrote = True
    except duckdb.Error:
        wrote = False
    return ticker, n, lake.bar_backend, lake.read_only, wrote, os.getpid()


def _open(spec):
    return spec.open() if isinstance(spec, DatasetSpec) else spec


def test_snapshot_of_a_parquet_lake_links_the_universe_partitions(parquet_lake):
    end = pd.bdate_range("2024-01-02", periods=160)[99].date()
    with LakeSnapshot.build(parquet_lake, ["A.US", "B.US"], end=end) as snap:
        bars_dir = snap.path.parent / "bars"
        assert bars_dir.is_dir()
        with snap.open() as lake:
            assert lake.bar_backend == "parquet" and lake.read_only
            got = lake.sql(
                "SELECT ticker, MAX(timestamp) AS hi, COUNT(*) AS n FROM bars GROUP BY 1"
            )
            assert sorted(got["ticker"]) == ["A.US", "B.US"]
            assert set(got["n"]) == {100}
            assert all(pd.Timestamp(t).date() <= end for t in got["hi"])
            assert len(lake.sql("SELECT * FROM prices")) == 200
            # the snapshot's DuckDB file holds no bars table: bars come from files
            tables = lake.sql(
                "SELECT table_name FROM duckdb_tables() WHERE database_name = current_database()"
            )
            assert "bars" not in set(tables["table_name"])
        # writes to the source after the build do not reach the snapshot
        parquet_lake.upsert_prices(
            parquet_lake.get_prices("A.US", end, end).assign(close=-1.0, adj_close=-1.0)
        )
        with snap.open() as lake:
            assert (lake.sql("SELECT close FROM bars WHERE ticker = 'A.US'")["close"] > 0).all()
    assert not snap.path.parent.exists()


def test_workers_read_the_parquet_snapshot_read_only(parquet_lake):
    start, end = lake_dates(parquet_lake)
    ds = LabDataset(lake=parquet_lake, universe=UNIVERSE, start=start, end=end)
    with dataset_snapshot(ds) as spec:
        out = run_tasks(_worker_task, UNIVERSE, setup=_open, payload=spec, max_workers=2)
    assert [(t, n, b, ro, w) for t, n, b, ro, w, _ in out] == [
        (t, 160, "parquet", True, False) for t in UNIVERSE
    ]
    assert os.getpid() not in {pid for *_, pid in out}
    # the source lake is still writable afterwards
    parquet_lake.upsert_prices(parquet_lake.get_prices("A.US", start, start))


def _backtest(lake) -> pd.Series:
    start, end = lake_dates(lake)
    config = BacktestConfig(start=start, end=end, universe=UNIVERSE)
    broker = SimulatedBroker(
        portfolio=Portfolio(cash=100_000.0, positions={}), slippage_bps=5.0, fee_per_trade=1.0
    )
    return pd.Series(Backtester([Momentum({})], broker, lake, config).run().equity_curve)


def test_backtests_are_bit_identical_on_both_bar_stores(tmp_path):
    curves = {}
    for backend in ("duckdb", "parquet"):
        lake = _lake(tmp_path, backend)
        try:
            assert lake.bar_backend == backend
            curves[backend] = _backtest(lake)
        finally:
            lake.close()
    assert len(curves["duckdb"]) > 100
    pd.testing.assert_series_equal(curves["duckdb"], curves["parquet"], check_exact=True)


def test_parallel_tuning_is_bit_identical_on_both_bar_stores(tmp_path):
    results = {}
    for backend in ("duckdb", "parquet"):
        lake = _lake(tmp_path, backend)
        try:
            start, end = lake_dates(lake)
            ds = LabDataset(lake=lake, universe=UNIVERSE, start=start, end=end, train_ratio=0.7)
            tuner = GridTuner(grid_size=3, seed=4, parallel=ParallelSettings(max_workers=2))
            results[backend] = tuner.tune(
                strategy_cls=Momentum,
                param_space=Momentum.parameter_spec(),
                objective=SharpeObjective(),
                dataset=ds,
                budget=4,
            )
        finally:
            lake.close()
    a, b = results["duckdb"], results["parquet"]
    assert a.trials == b.trials
    assert a.best_params == b.best_params and a.best_score == b.best_score
    assert all(t.n_bars > 0 for t in a.trials)

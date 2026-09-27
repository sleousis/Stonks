"""``pool_correlation`` survival test (BL-47, P17): a candidate must bring
something the active pool lacks."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from stonks.core.interval import Interval
from stonks.lab.dataset import LabDataset
from stonks.lab.survival.pool_correlation import PoolCorrelationTest
from stonks.lab.survival.registry import build_survival_test, survival_test_names
from stonks.registry.store import StrategyRegistry
from stonks.store.lake import DuckDBLake
from stonks.store.state import SqliteState
from stonks.strategies.examples.buy_and_hold import BuyAndHold

DAYS = [d.date() for d in pd.bdate_range("2023-01-02", periods=200)]


def _upsert(lake, ticker, closes):
    closes = np.asarray(closes, dtype=float)
    lake.upsert_prices(
        pd.DataFrame(
            {
                "ticker": ticker,
                "date": DAYS,
                "open": closes,
                "high": closes,
                "low": closes,
                "close": closes,
                "adj_close": closes,
                "volume": 1_000_000.0,
            }
        )
    )


@pytest.fixture
def lake(tmp_path):
    lake = DuckDBLake(tmp_path / "lake.duckdb")
    lake.migrate()
    rng = np.random.default_rng(4)
    n = len(DAYS)
    market = rng.normal(0.0, 0.01, n)
    noise = rng.normal(0.0, 0.002, n)
    # WIN and TWIN share the market and the noise; TWIN lacks WIN's drift.
    _upsert(lake, "WIN.US", 100 * np.cumprod(1 + market + noise + 0.002))
    _upsert(lake, "TWIN.US", 100 * np.cumprod(1 + market + noise))
    _upsert(lake, "IND.US", 100 * np.cumprod(1 + rng.normal(0.0005, 0.01, n)))
    yield lake
    lake.close()


def _dataset(lake):
    return LabDataset(
        lake=lake,
        universe=["WIN.US", "TWIN.US", "IND.US"],
        start=DAYS[0],
        end=DAYS[-1],
        interval=Interval.DAY_1,
    )


def _bah(ticker: str, allocation: float = 0.9) -> BuyAndHold:
    return BuyAndHold({"ticker": ticker, "allocation": allocation})


def test_the_test_is_registered():
    assert "pool_correlation" in survival_test_names()
    test = build_survival_test("pool_correlation", {"max_correlation": 0.8})
    assert isinstance(test, PoolCorrelationTest)


def test_a_candidate_that_copies_a_pool_member_fails(lake):
    test = PoolCorrelationTest(pool=[("bah_win", _bah("WIN.US", 0.5))])
    report = test.run(_bah("WIN.US"), _dataset(lake))
    assert report.passed is False
    assert report.metrics["max_correlation"] > 0.99
    assert report.metrics["pool_size"] == 1.0
    assert "bah_win" in report.notes


def test_an_uncorrelated_candidate_passes(lake):
    test = PoolCorrelationTest(pool=[("bah_ind", _bah("IND.US"))])
    report = test.run(_bah("WIN.US"), _dataset(lake))
    assert report.passed is True
    assert abs(report.metrics["max_correlation"]) < 0.7


def test_a_correlated_candidate_with_a_clearly_better_ir_passes(lake):
    test = PoolCorrelationTest(pool=[("bah_twin", _bah("TWIN.US"))])
    report = test.run(_bah("WIN.US"), _dataset(lake))
    assert report.metrics["max_correlation"] > 0.7
    assert report.metrics["candidate_ir"] >= 1.1 * report.metrics["member_ir:bah_twin"]
    assert report.passed is True


def test_the_worse_twin_fails_against_the_better_one(lake):
    test = PoolCorrelationTest(pool=[("bah_win", _bah("WIN.US"))])
    report = test.run(_bah("TWIN.US"), _dataset(lake))
    assert report.passed is False


def test_an_empty_pool_passes_with_a_note(lake):
    report = PoolCorrelationTest(pool=[]).run(_bah("WIN.US"), _dataset(lake))
    assert report.passed is True and "no active" in report.notes


def test_the_candidate_itself_is_not_its_own_pool_member(lake):
    same = _bah("WIN.US")
    report = PoolCorrelationTest(pool=[("bah_win", _bah("WIN.US"))]).run(same, _dataset(lake))
    assert report.passed is True and report.metrics["pool_size"] == 0.0


def test_the_default_pool_is_the_registry_active_list(lake, tmp_path):
    with SqliteState(tmp_path / "state.sqlite") as state:
        state.migrate()
        registry = StrategyRegistry(state=state, artifacts_dir=tmp_path / "artifacts")
        registry.register(_bah("WIN.US", 0.5), [], "bah_win")
        registry.set_status(
            "bah_win", "active", actor="test", reason="seeded as the active pool", override=True
        )
        registry.register(_bah("IND.US"), [], "bah_ind_shadow")
        test = PoolCorrelationTest(registry=registry)
        report = test.run(_bah("TWIN.US", 0.8), _dataset(lake))
    assert report.metrics["pool_size"] == 1.0
    assert "member_ir:bah_win" in report.metrics


# ---- nothing compared is not a pass (BE-26) -------------------------------------------


class _Broken(BuyAndHold):
    def estimate_return(self, ticker, as_of, lake):
        raise RuntimeError("boom")


def test_a_pool_whose_only_member_fails_to_backtest_fails(lake):
    report = PoolCorrelationTest(pool=[("broken", _Broken({"ticker": "IND.US"}))]).run(
        _bah("WIN.US"), _dataset(lake)
    )
    assert report.passed is False
    assert "insufficient data" in report.notes


def test_a_member_that_never_trades_the_window_is_not_a_pass(lake):
    report = PoolCorrelationTest(pool=[("ghost", _bah("NONE.US"))]).run(
        _bah("WIN.US"), _dataset(lake)
    )
    assert report.passed is False and "insufficient data" in report.notes


def test_a_member_is_backtested_on_its_own_universe(lake):
    """A member registered on TWIN.US is compared on TWIN.US even when the
    candidate's universe lacks it."""
    candidate_only = LabDataset(
        lake=lake, universe=["WIN.US"], start=DAYS[0], end=DAYS[-1], interval=Interval.DAY_1
    )
    test = PoolCorrelationTest(pool=[("bah_twin", _bah("TWIN.US"), ["TWIN.US"])])
    report = test.run(_bah("WIN.US"), candidate_only)
    assert report.metrics["correlation:bah_twin"] > 0.7


def test_the_registry_pool_reads_each_members_universe(lake, tmp_path):
    from stonks.registry.artifact import update_meta

    with SqliteState(tmp_path / "state.sqlite") as state:
        state.migrate()
        registry = StrategyRegistry(state=state, artifacts_dir=tmp_path / "artifacts")
        registry.register(_bah("TWIN.US", 0.5), [], "bah_twin")
        registry.set_status(
            "bah_twin", "active", actor="test", reason="seeded as the active pool", override=True
        )
        handle = registry.list_active()[0]
        update_meta(
            registry.resolve_artifact_path(str(handle.artifact_path)),
            {"manifest": {"dataset": {"universe": ["TWIN.US"]}}},
        )
        candidate_only = LabDataset(
            lake=lake, universe=["WIN.US"], start=DAYS[0], end=DAYS[-1], interval=Interval.DAY_1
        )
        report = PoolCorrelationTest(registry=registry).run(_bah("WIN.US"), candidate_only)
    assert report.metrics["correlation:bah_twin"] > 0.7

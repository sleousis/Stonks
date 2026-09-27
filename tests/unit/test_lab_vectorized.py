"""The vectorised pre-screen (BL-49, ``lab/vectorized.py``)."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from stonks.backtest.engine import BacktestConfig, Backtester
from stonks.backtest.simulated_broker import SimulatedBroker
from stonks.core.params import ParameterSpec
from stonks.core.types import Portfolio
from stonks.lab.dataset import LabDataset
from stonks.lab.vectorized import (
    SCREENED_OUT,
    PrescreenTuner,
    load_closes,
    prescreen,
    supports_vectorized,
    vectorized_backtest,
)
from stonks.store.lake import DuckDBLake
from stonks.strategies.examples.momentum import Momentum

DAYS = pd.bdate_range("2024-01-02", periods=160)
TICKERS = ["A.US", "B.US", "C.US"]


def _closes(seed: int = 5) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    data = {
        t: 50.0 * np.exp(np.cumsum(rng.normal(0.0005 * (i + 1), 0.02, len(DAYS))))
        for i, t in enumerate(TICKERS)
    }
    return pd.DataFrame(data, index=DAYS)


@pytest.fixture
def lake(tmp_path):
    """Bars whose open is the previous close, so a fill at the next open is
    a fill at the decision close (the vectorised model's assumption)."""
    closes = _closes()
    lake = DuckDBLake(tmp_path / "lake.duckdb")
    lake.migrate()
    rows = []
    for t in TICKERS:
        c = closes[t].to_numpy()
        o = np.concatenate([[c[0]], c[:-1]])
        rows.append(
            pd.DataFrame(
                {
                    "ticker": t,
                    "date": [d.date() for d in DAYS],
                    "open": o,
                    "high": np.maximum(o, c),
                    "low": np.minimum(o, c),
                    "close": c,
                    "adj_close": c,
                    "volume": 1e9,
                }
            )
        )
    lake.upsert_prices(pd.concat(rows, ignore_index=True))
    for t in TICKERS:
        lake.con.execute("INSERT INTO instruments (id, asset_class) VALUES (?, 'equity')", [t])
    yield lake
    lake.close()


# ---- the approximate backtest ---------------------------------------------------


def test_weights_earn_the_next_bars_return_less_turnover_costs():
    closes = pd.DataFrame({"X": [100.0, 110.0, 99.0]}, index=DAYS[:3])
    weights = pd.DataFrame({"X": [1.0, 1.0, 0.0]}, index=DAYS[:3])
    free = vectorized_backtest(closes, weights)
    assert free.returns.tolist() == pytest.approx([0.0, 0.10, -0.10])
    assert free.total_return == pytest.approx(1.1 * 0.9 - 1.0)
    costly = vectorized_backtest(closes, weights, cost_bps=100.0)
    # the buy at bar 0 is charged on bar 1, the sell at bar 2 after the end
    assert costly.returns.tolist() == pytest.approx([0.0, 0.09, -0.10])
    assert costly.turnover == pytest.approx(2.0)
    later = vectorized_backtest(closes, weights, start=DAYS[1])
    assert len(later.returns) == 2
    with pytest.raises(ValueError):
        vectorized_backtest(closes, weights, cost_bps=-1.0)


def test_sharpe_is_nan_without_variation():
    closes = pd.DataFrame({"X": [100.0, 100.0, 100.0]}, index=DAYS[:3])
    flat = vectorized_backtest(closes, closes * 0.0)
    assert math.isnan(flat.sharpe())
    assert math.isnan(vectorized_backtest(closes.iloc[:1], closes.iloc[:1]).sharpe())


def test_momentum_weights_never_read_later_rows():
    closes = _closes()
    params = {"lookback_days": 20, "skip_days": 0, "threshold": 0.0}
    full = Momentum.target_positions(closes, params)
    cut = 100
    shocked = closes.copy()
    shocked.iloc[cut:] *= np.linspace(0.2, 5.0, len(closes) - cut)[:, None]
    again = Momentum.target_positions(shocked, params)
    pd.testing.assert_frame_equal(full.iloc[:cut], again.iloc[:cut])


@pytest.mark.parametrize("tickers", [["A.US"], TICKERS])
def test_momentum_matches_the_event_engine_on_a_zero_cost_fixture(lake, tickers):
    params = {"lookback_days": 20, "skip_days": 5, "threshold": 0.0}
    start, end = DAYS[40].date(), DAYS[-1].date()
    config = BacktestConfig(start=start, end=end, universe=tickers)
    broker = SimulatedBroker(Portfolio(cash=100_000.0))
    report = Backtester([Momentum(params)], broker, lake, config).run()
    engine = np.asarray(report.equity_curve, dtype=float)
    engine_returns = engine[1:] / engine[:-1] - 1.0

    closes = load_closes(lake, tickers, end)
    weights = Momentum.target_positions(closes, params)
    fast = vectorized_backtest(closes, weights, start=DAYS[41])
    assert len(fast.returns) == len(engine_returns)
    np.testing.assert_allclose(fast.returns.to_numpy(), engine_returns, atol=1e-9)


# ---- the screen and the tuner ---------------------------------------------------


def test_prescreen_scores_every_candidate_in_order():
    closes = _closes()
    candidates = [
        {"lookback_days": lb, "skip_days": 0, "threshold": 0.0, "allocation": 1.0}
        for lb in (10, 20, 40)
    ]
    screened = prescreen(Momentum, candidates, closes)
    assert [s.params["lookback_days"] for s in screened] == [10, 20, 40]
    assert all(math.isfinite(s.score) for s in screened)
    by_return = prescreen(Momentum, candidates, closes, metric="total_return")
    assert by_return[0].score != screened[0].score
    with pytest.raises(ValueError):
        prescreen(Momentum, candidates, closes, metric="calmar")


def test_prescreen_refuses_a_strategy_without_target_positions():
    class Plain:
        pass

    assert not supports_vectorized(Plain)
    assert supports_vectorized(Momentum)
    with pytest.raises(TypeError):
        prescreen(Plain, [{}], _closes())


def test_a_candidate_that_raises_scores_nan():
    class Broken:
        @classmethod
        def target_positions(cls, closes, params):
            raise RuntimeError("boom")

    [only] = prescreen(Broken, [{"a": 1}], _closes())
    assert math.isnan(only.score)


class _Sharpe:
    direction = "maximize"
    calls: list[dict] = []

    def score(self, strategy, dataset):
        type(self).calls.append(dict(strategy.params))
        return float(strategy.params["lookback_days"])  # a known full score


class _TwoAxis(Momentum):
    @classmethod
    def parameter_spec(cls):
        spec = {s.name: s for s in super().parameter_spec()}
        spec["lookback_days"] = ParameterSpec(
            name="lookback_days", kind="int", default=20, bounds=(10, 60)
        )
        return list(spec.values())


def test_the_tuner_runs_full_backtests_only_on_the_best_screened(lake):
    _Sharpe.calls = []
    dataset = LabDataset(lake=lake, universe=TICKERS, start=DAYS[0].date(), end=DAYS[-1].date())
    tuner = PrescreenTuner(grid_size=4, max_candidates=100, cost_bps=0.0)
    space = _TwoAxis.parameter_spec()
    result = tuner.tune(_TwoAxis, space, _Sharpe(), dataset, budget=3)
    screened = tuner.last_screen
    assert len(screened) == len(result.history) > 3
    full = [t for t in result.trials if t.status == "ok"]
    assert len(full) == 3 == len(_Sharpe.calls)
    out = [t for t in result.trials if t.status == "failed"]
    assert all(t.error.startswith(SCREENED_OUT) for t in out)
    # the winner comes from the full scores only
    assert result.best_score == max(t.score for t in full)
    assert result.best_params["lookback_days"] == max(c["lookback_days"] for c in _Sharpe.calls)
    # the kept candidates are the screen's best
    kept = [s.score for s, t in zip(screened, result.trials, strict=True) if t.status == "ok"]
    dropped = [s.score for s, t in zip(screened, result.trials, strict=True) if t.status != "ok"]
    assert min(kept) >= max(d for d in dropped if not math.isnan(d))


def test_a_strategy_without_target_positions_falls_back_to_the_grid(lake):
    from tests.fixtures.parallel_lab import NoisyParamStrategy

    dataset = LabDataset(lake=lake, universe=TICKERS, start=DAYS[0].date(), end=DAYS[-1].date())

    class _Const:
        direction = "maximize"

        def score(self, strategy, dataset):
            return 1.0

    tuner = PrescreenTuner(grid_size=2)
    result = tuner.tune(
        NoisyParamStrategy, NoisyParamStrategy.parameter_spec(), _Const(), dataset, budget=4
    )
    assert tuner.last_screen == []
    assert result.history and all(s == 1.0 for _, s in result.history)


def test_bad_tuner_settings_are_refused():
    with pytest.raises(ValueError):
        PrescreenTuner(grid_size=0)

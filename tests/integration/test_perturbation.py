"""Perturbation survival test: noise must reach every bar read path
(``get_bars``, ``get_prices`` and the engine's SQL), and stay fixed per bar
within one noise level."""

from __future__ import annotations

from datetime import date, timedelta

from stonks.core.interval import Interval
from stonks.lab.dataset import LabDataset
from stonks.lab.survival.perturbation import PerturbationTest
from stonks.strategies.base import BaseStrategy
from stonks.strategies.examples.buy_and_hold import BuyAndHold
from stonks.strategies.examples.momentum import Momentum

FIRST_DAY = date(2025, 10, 1)


def _dataset(lake, universe=("UP.US",)):
    return LabDataset(
        lake=lake,
        universe=list(universe),
        start=FIRST_DAY,
        end=date(2026, 4, 1),
        train_ratio=0.6,
    )


class _Spy(BaseStrategy):
    """Records, per lake object, the first closes it sees via both read APIs."""

    id = "spy_fake"
    seen: list[tuple[object, tuple[float, ...], tuple[float, ...]]] = []

    def estimate_return(self, ticker, as_of, lake):
        bars = lake.get_bars(ticker, Interval.DAY_1, start=FIRST_DAY, end=as_of)
        prices = lake.get_prices(ticker, FIRST_DAY, FIRST_DAY + timedelta(days=7))
        _Spy.seen.append(
            (
                lake,
                tuple(float(c) for c in bars["close"].head(3)),
                tuple(float(c) for c in prices["close"].head(3)),
            )
        )
        return

    def decide(self, my_picks, portfolio, prices, as_of):
        return []


def test_noise_reaches_get_bars_and_get_prices_and_is_fixed_per_bar(lake_trending):
    _Spy.seen = []
    PerturbationTest(noise_sigmas=[0.01], min_correlation=-1.0, seed=5).run(
        _Spy({}), _dataset(lake_trending)
    )
    real = [(b, p) for lake, b, p in _Spy.seen if lake is lake_trending]
    noisy = [(b, p) for lake, b, p in _Spy.seen if lake is not lake_trending]
    assert real
    assert noisy

    real_bars = real[-1][0]
    noisy_bars = {b for b, _ in noisy if len(b) == 3}
    noisy_prices = {p for _, p in noisy}
    # the same bar read repeatedly -> the same perturbed value
    assert len(noisy_bars) == 1
    assert len(noisy_prices) == 1
    # ... which actually differs from the real bar
    assert next(iter(noisy_bars)) != real_bars
    # both read APIs see the same perturbed series
    assert next(iter(noisy_prices)) == next(iter(noisy_bars))


def test_noise_reaches_engine_prices(lake_trending):
    # BuyAndHold never reads the lake itself; only the engine's valuation
    # can move its equity curve.
    report = PerturbationTest(noise_sigmas=[0.05], min_correlation=-1.0, seed=1).run(
        BuyAndHold({"ticker": "UP.US", "allocation": 1.0}), _dataset(lake_trending)
    )
    assert report.metrics["correlation_min"] < 0.9999


def test_perturbation_does_not_mutate_real_lake(lake_trending):
    before = lake_trending.sql("SELECT * FROM bars ORDER BY ticker, timestamp")
    PerturbationTest(noise_sigmas=[0.05], min_correlation=-1.0, seed=1).run(
        Momentum({"lookback_days": 10, "threshold": 0.0}),
        _dataset(lake_trending, universe=("UP.US", "DOWN.US", "FLAT.US")),
    )
    after = lake_trending.sql("SELECT * FROM bars ORDER BY ticker, timestamp")
    assert before.equals(after)
    assert "get_prices" not in vars(lake_trending)


def test_perturbation_default_seed_is_reproducible(lake_trending):
    def run():
        return PerturbationTest(noise_sigmas=[0.02], min_correlation=-1.0).run(
            Momentum({"lookback_days": 5, "threshold": 0.0}),
            _dataset(lake_trending, universe=("UP.US", "DOWN.US", "FLAT.US")),
        )

    assert run().metrics == run().metrics

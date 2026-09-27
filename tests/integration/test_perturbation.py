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
from tests.fixtures.pit import underlying

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
        lake = underlying(lake)  # the perturbed lake behind the engine's view
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


class _StatementSpy(BaseStrategy):
    """Records, per lake object, whether it could read non-bar tables."""

    id = "statement_spy_fake"
    seen: dict[object, tuple[int, int, int]] = {}

    def estimate_return(self, ticker, as_of, lake):
        lake = underlying(lake)
        _StatementSpy.seen[lake] = (
            len(lake.get_income_statement(ticker)),
            len(lake.get_dividends(ticker)),
            int(lake.sql("SELECT COUNT(*) AS n FROM instruments")["n"].iloc[0]),
        )
        return

    def decide(self, my_picks, portfolio, prices, as_of):
        return []


def test_perturbed_lake_carries_statements_dividends_and_instruments(lake_trending):
    lake_trending.con.execute(
        "INSERT INTO income_statement (ticker, period_end, frequency, revenue)"
        " VALUES ('UP.US', DATE '2025-06-30', 'Q', 10.0)"
    )
    lake_trending.con.execute(
        "INSERT INTO dividends (ticker, ex_date, amount) VALUES ('UP.US', DATE '2025-11-03', 1.0)"
    )
    _StatementSpy.seen = {}
    PerturbationTest(noise_sigmas=[0.01], min_correlation=-1.0, seed=5).run(
        _StatementSpy({}), _dataset(lake_trending)
    )
    noisy = {k: v for k, v in _StatementSpy.seen.items() if k is not lake_trending}
    assert noisy
    # universe is only UP.US: one instrument row, its statement and dividend
    assert set(noisy.values()) == {(1, 1, 1)}


class _Coin(BaseStrategy):
    """Holds UP.US on bars whose close has an even fourth decimal: any noise
    reshuffles the trades, but every run's equity still rises."""

    id = "coin_fake"

    def estimate_return(self, ticker, as_of, lake):
        lake = underlying(lake)  # the perturbed lake behind the engine's view
        bars = lake.get_bars(ticker, Interval.DAY_1, start=FIRST_DAY, end=as_of)
        if bars.empty:
            return None
        return 1.0 if int(float(bars["close"].iloc[-1]) * 1e4) % 2 == 0 else None

    def decide(self, my_picks, portfolio, prices, as_of):
        from stonks.core.types import Order

        held = portfolio.positions.get("UP.US", 0.0)
        if my_picks and held <= 0:
            qty = portfolio.cash / prices["UP.US"] * 0.99
            return [Order(f"coin:buy:{as_of}", "UP.US", "buy", qty, "market")]
        if not my_picks and held > 0:
            return [Order(f"coin:sell:{as_of}", "UP.US", "sell", held, "market")]
        return []


def test_rs10_different_trades_on_a_rising_curve_fail(lake_trending):
    report = PerturbationTest(noise_sigmas=[0.01], min_correlation=0.8, seed=3).run(
        _Coin({}), _dataset(lake_trending)
    )
    # the equity levels still move together, the per-bar returns do not
    assert report.metrics["level_correlation_min"] > 0.8
    assert report.metrics["correlation_min"] < 0.8
    assert not report.passed


def test_rs10_zero_noise_only_passes_with_perfect_correlation(lake_trending):
    report = PerturbationTest(noise_sigmas=(0.0,), seed=3).run(
        BuyAndHold({"ticker": "UP.US"}), _dataset(lake_trending)
    )
    assert report.passed
    assert report.metrics["correlation_min"] == 1.0
    assert report.metrics["levels_tested"] == 1.0

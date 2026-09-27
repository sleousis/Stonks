"""BL-49: the backtest engine hands strategies a point-in-time lake.

A strategy that "cheats" (reads the whole bar history whatever ``as_of``
says) sees only bars up to each decision, so a backtest on a lake with a
planted future gives the same result as on the lake without it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime

import numpy as np
import pandas as pd
import pytest

from stonks.backtest.engine import BacktestConfig, Backtester
from stonks.backtest.simulated_broker import SimulatedBroker
from stonks.core.interval import Interval
from stonks.core.types import Order, Portfolio
from stonks.store.lake import DuckDBLake
from stonks.store.pit import PointInTimeLake

DAYS = [d.date() for d in pd.bdate_range("2025-01-06", periods=80)]
END = DAYS[59]


class _Cheater:
    """Scores a ticker by the return from its first to its *latest* bar in
    the lake, ignoring ``as_of``; buys the best, sells when it turns."""

    id = "cheater"
    applicable_asset_classes = ("equity",)
    label_horizon_bars = 0
    required_history_bars = 0

    def __init__(self) -> None:
        self.seen: list[tuple[datetime, object]] = []

    def estimate_return(self, ticker, as_of, lake):
        self.seen.append((as_of, lake))
        bars = lake.get_bars(ticker, Interval.DAY_1, datetime(1900, 1, 1), datetime(2200, 1, 1))
        if len(bars) < 2:
            return None
        return float(bars["close"].iloc[-1] / bars["close"].iloc[0] - 1.0)

    def decide(
        self,
        my_picks: Sequence[tuple[float, str]],
        portfolio: Portfolio,
        prices: Mapping[str, float],
        as_of,
    ) -> list[Order]:
        orders = []
        best = my_picks[0][1] if my_picks else None
        for t, q in portfolio.positions.items():
            if t != best and q > 0:
                orders.append(Order(client_id=f"s:{t}:{as_of}", ticker=t, side="sell", quantity=q))
        if best and portfolio.positions.get(best, 0.0) <= 0:
            qty = 0.9 * portfolio.cash / prices[best]
            orders.append(
                Order(client_id=f"b:{best}:{as_of}", ticker=best, side="buy", quantity=qty)
            )
        return orders

    def fit(self, dataset) -> None:
        return None


def _lake(path, planted: bool) -> DuckDBLake:
    lake = DuckDBLake(path)
    lake.migrate()
    rng = np.random.default_rng(1)
    frames = []
    for i, t in enumerate(("A.US", "B.US")):
        close = 50.0 * np.exp(np.cumsum(rng.normal(0.0, 0.02, len(DAYS))))
        if planted:
            close[60:] = 1_000.0 if i == 0 else 0.01
        n = len(DAYS) if planted else 60
        frames.append(
            pd.DataFrame(
                {
                    "ticker": t,
                    "date": DAYS[:n],
                    "open": close[:n],
                    "high": close[:n],
                    "low": close[:n],
                    "close": close[:n],
                    "adj_close": close[:n],
                    "volume": 1e6,
                }
            )
        )
    lake.upsert_prices(pd.concat(frames, ignore_index=True))
    return lake


def _run(lake, strategy) -> list[float]:
    broker = SimulatedBroker(Portfolio(cash=10_000.0))
    config = BacktestConfig(start=DAYS[10], end=END, universe=["A.US", "B.US"])
    report = Backtester([strategy], broker, lake, config).run()
    return [round(v, 6) for v in report.equity_curve]


@pytest.fixture
def lakes(tmp_path):
    past, planted = _lake(tmp_path / "a.duckdb", False), _lake(tmp_path / "b.duckdb", True)
    yield past, planted
    past.close()
    planted.close()


def test_a_cheating_strategy_cannot_see_bars_after_the_decision(lakes):
    past, planted = lakes
    assert _run(past, _Cheater()) == _run(planted, _Cheater())


def test_the_engine_hands_strategies_a_view_at_each_decision_bar(lakes):
    past, _ = lakes
    cheater = _Cheater()
    _run(past, cheater)
    assert cheater.seen
    for as_of, lake in cheater.seen:
        assert isinstance(lake, PointInTimeLake)
        assert lake.as_of == as_of
        assert lake.decision_interval == Interval.DAY_1
    sessions = {id(lake.pit_session) for _, lake in cheater.seen}
    assert len(sessions) == 1  # one session per run

"""Interval-aware backtest engine.

Iterates bars in ``[start, end]`` at a configurable ``Interval`` (1m, 5m,
15m, 1h, 4h, 1d, 1w, …). For each bar, for each active strategy and each
ticker in the universe, it calls ``estimate_return``, builds a ranked
list, picks the winner(s), and lets the winning strategy ``decide`` on
orders. The same Broker Protocol is used in production, so strategy code
is identical in both worlds.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime

from stonks.backtest.report import BacktestReport, compute_report
from stonks.backtest.simulated_broker import SimulatedBroker
from stonks.core.interval import Interval
from stonks.core.protocols import Strategy
from stonks.core.timeutil import as_datetime, day_end, day_start
from stonks.logging import get_logger
from stonks.store.lake import DuckDBLake

_log = get_logger("stonks.backtest.engine")


@dataclass(frozen=True)
class BacktestConfig:
    start: date | datetime
    end: date | datetime
    universe: Sequence[str]
    interval: Interval = Interval.DAY_1
    threshold: float = 0.0
    #: Rebalance every N bars (at the configured interval). 1 = every bar.
    rebalance_every_bars: int = 1


@dataclass
class _BarPrices:
    as_of: datetime
    prices: dict[str, float] = field(default_factory=dict)


class Backtester:
    def __init__(
        self,
        strategies: Sequence[Strategy],
        broker: SimulatedBroker,
        lake: DuckDBLake,
        config: BacktestConfig,
    ) -> None:
        self._strategies = list(strategies)
        self._broker = broker
        self._lake = lake
        self._config = config

    def run(self) -> BacktestReport:
        timestamps = self._trading_timestamps()
        equity_dates: list[datetime] = []
        equity_curve: list[float] = []

        bars_since_rebalance: int | None = None
        for as_of in timestamps:
            day_prices = self._prices_on(as_of)
            self._broker.set_prices(day_prices.prices, as_of=as_of)

            # Rebalance cadence — counted in bars, not calendar days, so this
            # is identical for daily and intraday intervals.
            if bars_since_rebalance is None or bars_since_rebalance >= self._config.rebalance_every_bars:
                self._rebalance(as_of, day_prices.prices)
                bars_since_rebalance = 1
            else:
                bars_since_rebalance += 1

            portfolio = self._broker.fetch_portfolio()
            equity = portfolio.total_value(day_prices.prices)
            equity_dates.append(as_of)
            equity_curve.append(equity)

        strategy_id = ",".join(s.id for s in self._strategies) or "empty"
        return compute_report(strategy_id, equity_dates, equity_curve)

    # ---- internals ----------------------------------------------------------

    def _trading_timestamps(self) -> list[datetime]:
        start_ts, end_ts = _to_window_bounds(self._config.start, self._config.end)
        df = self._lake.sql(
            """
            SELECT DISTINCT timestamp FROM bars
             WHERE ticker = ANY(?) AND interval = ?
               AND timestamp BETWEEN ? AND ?
             ORDER BY timestamp
            """,
            [
                list(self._config.universe),
                self._config.interval.code,
                start_ts,
                end_ts,
            ],
        )
        if df.empty:
            return []
        return [as_datetime(t) for t in df["timestamp"]]

    def _prices_on(self, as_of: datetime) -> _BarPrices:
        df = self._lake.sql(
            """
            SELECT ticker, close FROM bars
             WHERE ticker = ANY(?) AND interval = ? AND timestamp = ?
            """,
            [list(self._config.universe), self._config.interval.code, as_of],
        )
        prices = {row.ticker: float(row.close) for row in df.itertuples(index=False)}
        return _BarPrices(as_of=as_of, prices=prices)

    def _rebalance(self, as_of: datetime, prices: dict[str, float]) -> None:
        picks_by_strategy: dict[str, list[tuple[float, str]]] = {s.id: [] for s in self._strategies}
        for strategy in self._strategies:
            for ticker in self._config.universe:
                r = strategy.estimate_return(ticker, as_of, self._lake)
                if r is not None and r > self._config.threshold:
                    picks_by_strategy[strategy.id].append((r, ticker))

        portfolio = self._broker.fetch_portfolio()
        for strategy in self._strategies:
            picks = picks_by_strategy[strategy.id]
            if not picks:
                continue
            picks.sort(key=lambda p: p[0], reverse=True)
            orders = strategy.decide(picks, portfolio, prices, as_of)
            for order in orders:
                self._broker.place_order(order)


def _to_window_bounds(start, end) -> tuple[datetime, datetime]:
    return day_start(start), day_end(end)

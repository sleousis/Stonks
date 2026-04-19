"""Date-driven backtest engine.

Drives ``Strategy`` + ``Broker`` across a date range using prices read from
the lake. For each trading day, for each active strategy and each ticker in
the universe, it calls ``estimate_return``, builds a ranked list, picks the
winner(s), and lets the winning strategy ``decide`` on orders. The same
Broker Protocol is used in production, so strategy code is identical in
both worlds.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date

from stonks.backtest.report import BacktestReport, compute_report
from stonks.backtest.simulated_broker import SimulatedBroker
from stonks.core.protocols import Strategy
from stonks.logging import get_logger
from stonks.store.lake import DuckDBLake

_log = get_logger("stonks.backtest.engine")


@dataclass(frozen=True)
class BacktestConfig:
    start: date
    end: date
    universe: Sequence[str]
    threshold: float = 0.0
    rebalance_every_days: int = 1


@dataclass
class _DayPrices:
    as_of: date
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
        trading_days = self._trading_days()
        equity_dates: list[date] = []
        equity_curve: list[float] = []

        last_rebalance: date | None = None
        for as_of in trading_days:
            day_prices = self._prices_on(as_of)
            self._broker.set_prices(day_prices.prices, as_of=as_of)

            # rebalance cadence
            do_rebalance = (
                last_rebalance is None
                or (as_of - last_rebalance).days >= self._config.rebalance_every_days
            )
            if do_rebalance:
                self._rebalance(as_of, day_prices.prices)
                last_rebalance = as_of

            portfolio = self._broker.fetch_portfolio()
            equity = portfolio.total_value(day_prices.prices)
            equity_dates.append(as_of)
            equity_curve.append(equity)

        strategy_id = ",".join(s.id for s in self._strategies) or "empty"
        return compute_report(strategy_id, equity_dates, equity_curve)

    # ---- internals ----------------------------------------------------------

    def _trading_days(self) -> list[date]:
        df = self._lake.sql(
            """
            SELECT DISTINCT date FROM prices
             WHERE ticker = ANY(?)
               AND date BETWEEN ? AND ?
             ORDER BY date
            """,
            [list(self._config.universe), self._config.start, self._config.end],
        )
        if df.empty:
            return []
        return [d.date() if hasattr(d, "date") else d for d in df["date"]]

    def _prices_on(self, as_of: date) -> _DayPrices:
        df = self._lake.sql(
            "SELECT ticker, close FROM prices WHERE ticker = ANY(?) AND date = ?",
            [list(self._config.universe), as_of],
        )
        prices = {row.ticker: float(row.close) for row in df.itertuples(index=False)}
        return _DayPrices(as_of=as_of, prices=prices)

    def _rebalance(self, as_of: date, prices: dict[str, float]) -> None:
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

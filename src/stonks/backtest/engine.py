"""Interval-aware backtest engine.

Iterates bars in ``[start, end]`` at a configurable ``Interval`` (1m, 5m,
15m, 1h, 4h, 1d, 1w, …). On each rebalance bar, for each strategy and each
ticker in the universe, it calls ``estimate_return``, keeps the tickers
above ``threshold`` as that strategy's ranked picks, and calls ``decide``
— always, even when the picks are empty, so exit logic in the no-picks
branch runs. The same Broker Protocol is used in production, so strategy
code is identical in both worlds.

Execution convention (no look-ahead)
------------------------------------
Signals at bar ``t`` see data up to and including bar ``t``'s close, so
orders decided at bar ``t`` are queued and fill at the **open of the next
bar** in which that ticker has a bar. Orders still queued when the next
rebalance decides are replaced by the fresh decisions; orders still queued
after the last bar are never filled. Per bar the engine:

1. fills queued orders at this bar's opens (``broker.set_prices(opens)``),
2. sets the broker's prices to closes (carried forward per ticker from its
   last bar when it has no bar at this timestamp, e.g. equities on a
   crypto weekend),
3. on rebalance bars, asks strategies to ``decide`` and queues the orders,
4. marks equity at those (carried-forward) closes.

All bar prices for the universe / interval / window are loaded with one
query up front, joined to ``instruments`` for each ticker's asset class
(missing row or NULL -> ``equity``).

Annualization
-------------
Sharpe uses ``periods_per_year(interval, asset_classes)`` over the asset
classes of the tickers that actually have bars in the window (a universe
ticker with no bars adds no equity-curve points). A mixed universe uses the
densest calendar present, since equity is marked on the union of bar
timestamps; see ``stonks.backtest.calendar``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime

import pandas as pd

from stonks.backtest.report import BacktestReport, compute_report, periods_per_year
from stonks.backtest.simulated_broker import SimulatedBroker
from stonks.core.interval import Interval
from stonks.core.protocols import Strategy
from stonks.core.timeutil import as_datetime, day_end, day_start
from stonks.core.types import AssetClass, Order
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


@dataclass(frozen=True)
class _Bar:
    open: float | None
    close: float


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
        bars_by_ts, asset_classes = self._load_bars()
        equity_dates: list[datetime] = []
        equity_curve: list[float] = []
        last_close: dict[str, float] = {}
        pending: list[Order] = []

        bars_since_rebalance: int | None = None
        for as_of, bars in bars_by_ts.items():
            # 1. fill orders queued on a previous bar at this bar's open
            if pending:
                pending = self._fill_pending(pending, bars, as_of)

            # 2. mark-to-market prices: this bar's closes, carried forward
            for ticker, bar in bars.items():
                last_close[ticker] = bar.close
            marks = dict(last_close)
            self._broker.set_prices(marks, as_of=as_of)

            # 3. rebalance cadence — counted in bars, not calendar days, so
            # this is identical for daily and intraday intervals.
            if (
                bars_since_rebalance is None
                or bars_since_rebalance >= self._config.rebalance_every_bars
            ):
                pending = self._decide(as_of, marks)
                bars_since_rebalance = 1
            else:
                bars_since_rebalance += 1

            # 4. equity at close
            portfolio = self._broker.fetch_portfolio()
            equity_dates.append(as_of)
            equity_curve.append(portfolio.total_value(marks))

        if pending:
            _log.debug("unfilled_orders_at_end", count=len(pending))
        strategy_id = ",".join(s.id for s in self._strategies) or "empty"
        return compute_report(
            strategy_id,
            equity_dates,
            equity_curve,
            periods_per_year=periods_per_year(self._config.interval, set(asset_classes.values())),
        )

    # ---- internals ----------------------------------------------------------

    def _load_bars(
        self,
    ) -> tuple[dict[datetime, dict[str, _Bar]], dict[str, AssetClass]]:
        """All bars for the universe / interval / window, keyed by timestamp
        (ascending) then ticker, plus the asset class of every ticker that
        has bars (from ``instruments``; no row or NULL means equity). One
        query for the whole run."""
        start_ts, end_ts = _to_window_bounds(self._config.start, self._config.end)
        df = self._lake.sql(
            """
            SELECT b.timestamp, b.ticker, b.open, b.close,
                   COALESCE(i.asset_class, 'equity') AS asset_class
              FROM bars b
              LEFT JOIN instruments i ON i.id = b.ticker
             WHERE b.ticker = ANY(?) AND b.interval = ?
               AND b.timestamp BETWEEN ? AND ?
             ORDER BY b.timestamp, b.ticker
            """,
            [
                list(self._config.universe),
                self._config.interval.code,
                start_ts,
                end_ts,
            ],
        )
        out: dict[datetime, dict[str, _Bar]] = {}
        asset_classes: dict[str, AssetClass] = {}
        for row in df.itertuples(index=False):
            asset_classes[row.ticker] = row.asset_class
            open_ = None if pd.isna(row.open) else float(row.open)
            out.setdefault(as_datetime(row.timestamp), {})[row.ticker] = _Bar(
                open=open_, close=float(row.close)
            )
        return out, asset_classes

    def _fill_pending(
        self, pending: list[Order], bars: dict[str, _Bar], as_of: datetime
    ) -> list[Order]:
        """Fill queued orders whose ticker has an open at this bar; return
        the orders still waiting for their ticker's next bar."""
        opens = {t: b.open for t, b in bars.items() if b.open is not None and b.open > 0}
        self._broker.set_prices(opens, as_of=as_of)
        waiting: list[Order] = []
        for order in pending:
            if order.ticker in opens:
                self._broker.place_order(order)
            else:
                waiting.append(order)
        return waiting

    def _decide(self, as_of: datetime, prices: dict[str, float]) -> list[Order]:
        """Picks and orders are keyed by the strategy's *position* in the
        engine, not its class-level ``id``, so two instances of one class
        (different params) keep separate picks; each order's ``client_id``
        is prefixed with ``"<index>:"`` so the broker's idempotency check
        can't drop one instance's order as a duplicate of the other's."""
        picks_by_strategy: list[list[tuple[float, str]]] = [[] for _ in self._strategies]
        for index, strategy in enumerate(self._strategies):
            for ticker in self._config.universe:
                r = strategy.estimate_return(ticker, as_of, self._lake)
                if r is not None and r > self._config.threshold:
                    picks_by_strategy[index].append((r, ticker))

        portfolio = self._broker.fetch_portfolio()
        orders: list[Order] = []
        for index, strategy in enumerate(self._strategies):
            picks = picks_by_strategy[index]
            picks.sort(key=lambda p: p[0], reverse=True)
            orders.extend(
                replace(order, client_id=f"{index}:{order.client_id}")
                for order in strategy.decide(picks, portfolio, prices, as_of)
            )
        return orders


def _to_window_bounds(start, end) -> tuple[datetime, datetime]:
    return day_start(start), day_end(end)

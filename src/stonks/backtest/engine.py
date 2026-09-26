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

0. applies corporate actions whose ex-date has arrived (see below),
1. fills queued orders at this bar's opens (``broker.set_prices(opens)``),
2. sets the broker's prices to closes (carried forward per ticker from its
   last bar when it has no bar at this timestamp, e.g. equities on a
   crypto weekend),
3. on rebalance bars, asks strategies to ``decide`` and queues the orders,
4. marks equity at those (carried-forward) closes.

All bar prices for the universe / interval / window are loaded with one
query up front, joined to ``instruments`` for each ticker's asset class
(missing row or NULL -> ``equity``). The broker receives those asset
classes and, when filling, each fill bar's open, high, low and volume, so
its ``FillModel`` can cap participation and honour limit / stop orders and
its ``CostModel`` can charge per-asset-class fees and volume-aware
slippage.

Execution realism (BL-30, BL-31)
--------------------------------
When the broker's fill or cost model needs lagged statistics
(``broker.market_stats_spec`` is not ``None``), the same single query also
loads up to ``spec.lookback_bars`` warm-up bars per ticker before
``start`` (never iterated, never on the equity curve), and
``stonks.backtest.fills.lagged_market_stats`` computes each bar's ADV,
sigma and spread estimate from **prior** bars only (a one-bar shift), once
per run. Each fill bar hands those to the broker with its prices.

Orders are placed with their decision time (``decided_at``) for the fill
model's gap guard. Whatever the fill model defers (participation cap, zero
volume; ``broker.unfilled_quantity``) is re-queued for the ticker's next
bar as a child order ``"<client_id>~<n>"`` (distinct ids keep the broker's
idempotency and give the trade ledger one fill per child); the next
rebalance replaces it like any queued order. With the default broker
nothing is deferred and nothing below changes.

Corporate actions
-----------------
Fills and marks use **raw** prices (what the market quoted); strategies
read split/dividend-adjusted history through their bar accessors. The
universe's splits and cash dividends are loaded once per run from a
``CorporateActionsProvider`` (default: the lake's ``stock_splits`` and
``dividends`` tables). On the first bar of a ticker dated on or after an
ex-date, before that bar's fills, a split multiplies the held quantity
(and queued orders for the ticker) by its ratio and a dividend credits
``quantity x amount x (1 - dividend_withholding_rate)`` as cash; each
applied event is listed in ``BacktestReport.corporate_actions`` so equity
jumps are explainable. A ticker with no corporate-action rows behaves
exactly as before. See ``stonks.backtest.corporate_actions``.

Construction (BL-12)
--------------------
With ``BacktestConfig.construction`` unset, each strategy decides against
the shared portfolio exactly as above. Set to a constructor name (or
:class:`~stonks.portfolio.pipeline.ConstructionSettings`), every rebalance
runs the same pipeline as the production tick
(:func:`stonks.portfolio.pipeline.build_orders`): the strategies' signals
are combined by the constructor (``strategy_weights`` in strategy order,
equal by default), diffed into orders with the no-trade buffer and passed
through every registered risk rule when ``BacktestConfig.risk`` is set, with
a ``RiskContext`` from the engine's own equity curve, daily history and
the entry date of each held position (from the broker's fills, so
``max_holding`` works in a backtest). The history a decision sees ends at
its bar (daily) or the day before (intraday), never later. That history is
adjusted with the vendor's ``adj_close``, which already folds in splits and
dividends after the decision, so each slice is rebased to the raw close of
its last bar (RS-15): ATRs and volatilities come out in the units of the
shares the decision trades, as if adjusted on the decision day. A rule's order
carries a date-keyed client id, so the engine adds the bar time to it: two
forced exits on one intraday day stay two orders. Strategies are keyed ``"0"``, ``"1"``, ... by
position; each decision's target book is kept in ``target_books``.

Point-in-time membership (RS-05, P14)
-------------------------------------
With ``BacktestConfig.universe_id`` set, the engine reads that universe's
``universe_membership`` spans once and, on every decision bar, asks the
strategies (or the pipeline) only about the tickers that are members on
that day. Buys of any other ticker are dropped, and a holding that is no
longer a member is sold in full (client id ``universe:<bar>:<ticker>:sell``).
A universe ticker with no span in that universe never trades. Without a
``universe_id`` every universe ticker trades on every bar, as before.

Annualization
-------------
Sharpe uses ``periods_per_year(interval, asset_classes)`` over the asset
classes of the tickers that actually have bars in the window (a universe
ticker with no bars adds no equity-curve points). A mixed universe uses the
densest calendar present, since equity is marked on the union of bar
timestamps; see ``stonks.backtest.calendar``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING

import pandas as pd

from stonks.backtest.corporate_actions import (
    CorporateActionRecord,
    CorporateActionSchedule,
    adjust_orders_for_split,
    apply_to_portfolio,
)
from stonks.backtest.fills import (
    MarketStats,
    MarketStatsSpec,
    lagged_market_stats,
    market_stats_from_row,
)
from stonks.backtest.report import BacktestReport, compute_report, periods_per_year
from stonks.backtest.simulated_broker import SimulatedBroker
from stonks.core.corporate_actions import CorporateActionsProvider, Split
from stonks.core.interval import Interval
from stonks.core.protocols import Strategy
from stonks.core.timeutil import as_datetime, day_end, day_start
from stonks.core.types import AssetClass, Order, OrderSide
from stonks.logging import get_logger
from stonks.store.corporate_actions import LakeCorporateActions
from stonks.store.lake import DuckDBLake

if TYPE_CHECKING:
    # The pipeline imports ``stonks.config``, which imports the lab and so
    # this module: it is imported where it runs, never at import time.
    from stonks.config import RiskPolicy
    from stonks.portfolio.base import TargetBook
    from stonks.portfolio.pipeline import ConstructionSettings

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
    #: Fraction of each cash dividend withheld as tax (0 = credit in full).
    dividend_withholding_rate: float = 0.0
    #: ``None``: each strategy decides alone (see the module doc). A
    #: constructor name or settings: the production construction pipeline.
    construction: str | ConstructionSettings | None = None
    #: Capital share per strategy, in strategy order (``None`` = equal).
    strategy_weights: Sequence[float] | None = None
    #: The risk policy the pipeline applies (``None`` = no risk layer).
    risk: RiskPolicy | None = None
    #: Daily bars of history a pipeline decision may read.
    history_bars: int = 260
    #: Stored universe whose membership spans gate trading (see the module
    #: doc); ``None`` trades every universe ticker on every bar.
    universe_id: str | None = None

    def __post_init__(self) -> None:
        if not 0.0 <= self.dividend_withholding_rate <= 1.0:
            raise ValueError(
                f"dividend_withholding_rate must be in [0, 1], got {self.dividend_withholding_rate}"
            )
        if isinstance(self.construction, str):
            from stonks.portfolio.pipeline import ConstructionSettings

            object.__setattr__(self, "construction", ConstructionSettings(method=self.construction))

    @property
    def construction_settings(self) -> ConstructionSettings | None:
        # normalised to settings in __post_init__
        return self.construction  # type: ignore[return-value]


@dataclass(frozen=True)
class _Bar:
    open: float | None
    close: float
    #: Units traded in the bar; ``None`` when the vendor gave no volume.
    volume: float | None = None
    high: float | None = None
    low: float | None = None
    #: Lagged statistics for fills in this bar (only when a model needs them).
    stats: MarketStats | None = None


class Backtester:
    def __init__(
        self,
        strategies: Sequence[Strategy],
        broker: SimulatedBroker,
        lake: DuckDBLake,
        config: BacktestConfig,
        corporate_actions: CorporateActionsProvider | None = None,
    ) -> None:
        self._strategies = list(strategies)
        self._broker = broker
        self._lake = lake
        self._config = config
        self._corporate_actions = corporate_actions or LakeCorporateActions(lake)
        # order bookkeeping for one run: decision time per client id, and
        # the root id / part count of carried remainders
        self._decided_at: dict[str, datetime] = {}
        self._roots: dict[str, str] = {}
        self._parts: dict[str, int] = {}
        # construction-pipeline state for one run (see the module doc)
        self._asset_classes: dict[str, AssetClass] = {}
        self._history: dict[str, pd.DataFrame] = {}
        self._equity: list[tuple[datetime, float]] = []
        self._attribution: dict[str, dict[str, float]] = {}
        self._fill_owner: dict[str, tuple[int, str]] = {}
        self._fill_seq = 0
        self._membership: dict[str, list[tuple[date, date | None]]] | None = None
        self._raw_closes: dict[str, dict[date, float]] = {}
        #: The target book of every pipeline decision, by bar.
        self.target_books: dict[datetime, TargetBook] = {}
        #: The close each order was decided at, by client id (TCA, BL-32:
        #: ``stonks.production.tca.backtest_shortfalls`` prices the fills
        #: against it exactly as live orders are priced).
        self.decision_prices: dict[str, float] = {}

    def run(self) -> BacktestReport:
        self._decided_at, self._roots, self._parts = {}, {}, {}
        self._equity, self._attribution, self._fill_owner, self._fill_seq = [], {}, {}, 0
        self.target_books = {}
        self.decision_prices = {}
        spec = getattr(self._broker, "market_stats_spec", None)
        bars_by_ts, asset_classes = self._load_bars(spec)
        self._asset_classes = asset_classes
        self._history = self._load_history(bars_by_ts)
        self._broker.set_asset_classes(asset_classes)
        self._membership = self._load_membership()
        set_interval = getattr(self._broker, "set_interval", None)
        if callable(set_interval):
            set_interval(self._config.interval)
        schedule = CorporateActionSchedule(
            self._corporate_actions.load(list(self._config.universe))
        )
        applied: list[CorporateActionRecord] = []
        equity_dates: list[datetime] = []
        equity_curve: list[float] = []
        last_close: dict[str, float] = {}
        pending: list[Order] = []

        bars_since_rebalance: int | None = None
        for as_of, bars in bars_by_ts.items():
            # 0. corporate actions going ex on this bar, before its fills
            pending = self._apply_corporate_actions(schedule, bars, as_of, pending, applied)

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
                self._decided_at = {order.client_id: as_of for order in pending}
                for order in pending:
                    if order.ticker in marks:
                        self.decision_prices.setdefault(order.client_id, marks[order.ticker])
                bars_since_rebalance = 1
            else:
                bars_since_rebalance += 1

            # 4. equity at close
            portfolio = self._broker.fetch_portfolio()
            equity_dates.append(as_of)
            equity_curve.append(portfolio.total_value(marks))
            self._equity.append((as_of, equity_curve[-1]))

        if pending:
            _log.debug("unfilled_orders_at_end", count=len(pending))
        strategy_id = ",".join(s.id for s in self._strategies) or "empty"
        return compute_report(
            strategy_id,
            equity_dates,
            equity_curve,
            periods_per_year=periods_per_year(self._config.interval, set(asset_classes.values())),
            corporate_actions=applied,
        )

    # ---- internals ----------------------------------------------------------

    def _load_bars(
        self, spec: MarketStatsSpec | None = None
    ) -> tuple[dict[datetime, dict[str, _Bar]], dict[str, AssetClass]]:
        """All bars for the universe / interval / window, keyed by timestamp
        (ascending) then ticker, plus the asset class of every ticker that
        has bars (from ``instruments``; no row or NULL means equity). One
        query for the whole run. With a ``spec`` the query also returns up
        to ``spec.lookback_bars`` warm-up bars per ticker before the window,
        used only to compute each bar's lagged ``MarketStats``."""
        start_ts, end_ts = _to_window_bounds(self._config.start, self._config.end)
        universe = list(self._config.universe)
        interval = self._config.interval.code
        if spec is None:
            df = self._lake.sql(
                f"""
                SELECT {_BAR_COLUMNS}, TRUE AS in_window
                  FROM bars b
                  LEFT JOIN instruments i ON i.id = b.ticker
                 WHERE b.ticker = ANY(?) AND b.interval = ?
                   AND b.timestamp BETWEEN ? AND ?
                 ORDER BY b.timestamp, b.ticker
                """,
                [universe, interval, start_ts, end_ts],
            )
        else:
            df = self._lake.sql(
                f"""
                SELECT {_BAR_COLUMNS}, b.timestamp >= ? AS in_window
                  FROM bars b
                  LEFT JOIN instruments i ON i.id = b.ticker
                 WHERE b.ticker = ANY(?) AND b.interval = ? AND b.timestamp <= ?
                QUALIFY b.timestamp >= ?
                     OR ROW_NUMBER() OVER (
                            PARTITION BY b.ticker, b.timestamp >= ?
                            ORDER BY b.timestamp DESC
                        ) <= ?
                 ORDER BY b.timestamp, b.ticker
                """,
                [start_ts, universe, interval, end_ts, start_ts, start_ts, spec.lookback_bars],
            )
        stats = None
        if spec is not None and not df.empty:
            frame = lagged_market_stats(df, spec)
            stats = zip(
                frame["adv"].to_numpy(),
                frame["sigma_daily"].to_numpy(),
                frame["half_spread_bps"].to_numpy(),
                strict=True,
            )
        out: dict[datetime, dict[str, _Bar]] = {}
        asset_classes: dict[str, AssetClass] = {}
        for row in df.itertuples(index=False):
            row_stats = next(stats) if stats is not None else None
            if not row.in_window:
                continue
            asset_classes[row.ticker] = row.asset_class
            out.setdefault(as_datetime(row.timestamp), {})[row.ticker] = _Bar(
                open=_float(row.open),
                close=float(row.close),
                volume=_float(row.volume),
                high=_float(row.high),
                low=_float(row.low),
                stats=None if row_stats is None else market_stats_from_row(*row_stats),
            )
        return out, asset_classes

    def _apply_corporate_actions(
        self,
        schedule: CorporateActionSchedule,
        bars: dict[str, _Bar],
        as_of: datetime,
        pending: list[Order],
        applied: list[CorporateActionRecord],
    ) -> list[Order]:
        """Apply every event due for a ticker with a bar at ``as_of`` to the
        broker's portfolio (and splits to queued orders); returns the
        possibly re-sized queue and appends applied events to ``applied``."""
        bar_date = as_of.date()
        portfolio = self._broker.fetch_portfolio()
        for ticker in bars:
            for action in schedule.due(ticker, bar_date):
                if isinstance(action, Split):
                    pending = adjust_orders_for_split(pending, action)
                record = apply_to_portfolio(
                    portfolio,
                    action,
                    as_of,
                    withholding_rate=self._config.dividend_withholding_rate,
                )
                if record is not None:
                    _log.debug(
                        "corporate_action_applied",
                        ticker=ticker,
                        kind=record.kind,
                        value=record.value,
                        cash_delta=record.cash_delta,
                    )
                    applied.append(record)
        return pending

    def _fill_pending(
        self, pending: list[Order], bars: dict[str, _Bar], as_of: datetime
    ) -> list[Order]:
        """Fill queued orders whose ticker has an open at this bar; return
        the orders still waiting for their ticker's next bar, plus the
        remainders the fill model deferred.

        The broker sees this (fill) bar's volume, high and low. That is not
        look-ahead: they only price and bound the execution of a fill that
        happens inside this bar, and no strategy decision ever reads them.
        The ``MarketStats`` it sees come from earlier bars only."""
        opens = {t: b.open for t, b in bars.items() if b.open is not None and b.open > 0}
        volumes = {t: bars[t].volume for t in opens if bars[t].volume is not None}
        highs = {t: bars[t].high for t in opens if bars[t].high is not None}
        lows = {t: bars[t].low for t in opens if bars[t].low is not None}
        stats = {t: bars[t].stats for t in opens if bars[t].stats is not None}
        self._broker.set_prices(
            opens, as_of=as_of, volumes=volumes, highs=highs, lows=lows, stats=stats
        )
        waiting: list[Order] = []
        for order in pending:
            if order.ticker not in opens:
                waiting.append(order)
                continue
            fill = self._broker.place_order(order, decided_at=self._decided_at.get(order.client_id))
            if fill is not None and order.strategy_id is not None:
                self._fill_seq += 1
                self._fill_owner[order.ticker] = (self._fill_seq, order.strategy_id)
            rest = self._broker.unfilled_quantity(order.client_id)
            if rest > 0:
                waiting.append(self._carry(order, rest, as_of))
        return waiting

    def _carry(self, order: Order, quantity: float, as_of: datetime) -> Order:
        """The deferred ``quantity`` of ``order`` as a new child order."""
        root = self._roots.get(order.client_id, order.client_id)
        part = self._parts.get(root, 1) + 1
        self._parts[root] = part
        child = replace(order, client_id=f"{root}~{part}", quantity=quantity)
        self._roots[child.client_id] = root
        self._decided_at[child.client_id] = as_of
        return child

    def _decide(self, as_of: datetime, prices: dict[str, float]) -> list[Order]:
        members = self._members_on(as_of)
        tradable = [t for t in self._config.universe if members is None or t in members]
        if self._config.construction_settings is not None:
            orders = self._decide_with_pipeline(as_of, prices, tradable)
        else:
            orders = self._decide_per_strategy(as_of, prices, tradable)
        return orders if members is None else self._enforce_membership(orders, members, as_of)

    # ---- point-in-time membership (RS-05) -------------------------------------

    def _load_membership(self) -> dict[str, list[tuple[date, date | None]]] | None:
        """``ticker -> [(start, end)]`` spans of ``config.universe_id`` for
        the universe tickers (``end`` is the first day out, ``None`` open);
        ``None`` when no universe id is set."""
        universe_id = self._config.universe_id
        if universe_id is None:
            return None
        spans: dict[str, list[tuple[date, date | None]]] = {}
        reader = getattr(self._lake, "get_universe_membership", None)
        if not callable(reader):
            _log.warning("universe_membership_unavailable", universe_id=universe_id)
            return spans
        frame = reader(universe_id, list(self._config.universe))
        for row in frame.itertuples(index=False):
            end = None if pd.isna(row.end_date) else _as_date(row.end_date)
            spans.setdefault(str(row.ticker), []).append((_as_date(row.start_date), end))
        if not spans:
            _log.warning("universe_membership_empty", universe_id=universe_id)
        return spans

    def _members_on(self, as_of: datetime) -> set[str] | None:
        if self._membership is None:
            return None
        day = as_of.date() if isinstance(as_of, datetime) else as_of
        return {
            ticker
            for ticker, spans in self._membership.items()
            if any(start <= day and (end is None or day < end) for start, end in spans)
        }

    def _enforce_membership(
        self, orders: list[Order], members: set[str], as_of: datetime
    ) -> list[Order]:
        """Drop buys of non-members and sell every holding that left."""
        kept = [o for o in orders if o.side != "buy" or o.ticker in members]
        selling: dict[str, float] = {}
        for o in kept:
            if o.side == "sell":
                selling[o.ticker] = selling.get(o.ticker, 0.0) + o.quantity
        portfolio = self._broker.fetch_portfolio()
        for ticker, qty in sorted(portfolio.positions.items()):
            rest = qty - selling.get(ticker, 0.0)
            if ticker in members or rest <= 1e-12:
                continue
            kept.append(
                Order(
                    client_id=f"universe:{as_of.isoformat()}:{ticker}:sell",
                    ticker=ticker,
                    side="sell",
                    quantity=rest,
                    order_type="market",
                    strategy_id="universe",
                )
            )
        return kept

    def _decide_per_strategy(
        self, as_of: datetime, prices: dict[str, float], tradable: Sequence[str]
    ) -> list[Order]:
        """Picks and orders are keyed by the strategy's *position* in the
        engine, not its class-level ``id``, so two instances of one class
        (different params) keep separate picks; each order's ``client_id``
        is prefixed with ``"<index>:"`` so the broker's idempotency check
        can't drop one instance's order as a duplicate of the other's."""
        picks_by_strategy: list[list[tuple[float, str]]] = [[] for _ in self._strategies]
        for index, strategy in enumerate(self._strategies):
            for ticker in tradable:
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

    # ---- construction pipeline (BL-12) --------------------------------------

    def _decide_with_pipeline(
        self, as_of: datetime, prices: dict[str, float], tradable: Sequence[str]
    ) -> list[Order]:
        """The production pipeline over this bar's signals; see the module doc."""
        from stonks.portfolio.pipeline import (
            PORTFOLIO_STRATEGY,
            BookInput,
            MarketView,
            build_orders,
            vols_from_history,
        )
        from stonks.production.risk import entry_dates_from_fills
        from stonks.production.rules import RiskContext

        construction = self._config.construction_settings
        assert construction is not None
        keys = [str(i) for i in range(len(self._strategies))]
        signals: dict[str, dict[str, float]] = {}
        for key, strategy in zip(keys, self._strategies, strict=True):
            scores: dict[str, float] = {}
            for ticker in tradable:
                r = strategy.estimate_return(ticker, as_of, self._lake)
                if r is not None and r > self._config.threshold:
                    scores[ticker] = r
            signals[key] = scores
        portfolio = self._broker.fetch_portfolio()
        history = self._history_until(as_of)
        market = MarketView(
            as_of=as_of,
            prices=prices,
            asset_classes=self._asset_classes,
            vols_annual={} if construction.is_single_winner else vols_from_history(history),
        )
        weights = self._config.strategy_weights
        policy = self._config.risk
        context = None
        if policy is not None:
            context = RiskContext(
                portfolio=portfolio,
                prices=prices,
                asset_classes=self._asset_classes,
                policy=policy,
                history=history,
                equity_curve=[(ts.date(), value) for ts, value in self._equity],
                entry_dates=entry_dates_from_fills(
                    (f.ticker, f.side, f.quantity, as_datetime(f.filled_at).date())
                    for f in getattr(self._broker, "fills", ())
                ),
            )
        book = BookInput(
            portfolio=portfolio,
            construction=construction,
            risk=policy,
            strategy_weights=None if weights is None else dict(zip(keys, weights, strict=True)),
            prior_attribution=self._attribution,
            risk_context=context,
        )

        def client_id(strategy_id: str | None, ticker: str, side: OrderSide) -> str:
            return f"{strategy_id or PORTFOLIO_STRATEGY}:{as_of.isoformat()}:{ticker}:{side}"

        result = build_orders(
            signals,
            book,
            market,
            strategies=lambda key: self._strategies[int(key)],
            exit_owner=lambda: self._exit_owner(portfolio.positions),
            client_id=client_id,
        )
        self.target_books[as_of] = result.target_book
        self._attribution = {**self._attribution, **result.attribution}
        stamp = as_of.isoformat()
        return [
            o if stamp in o.client_id else replace(o, client_id=f"{o.client_id}@{stamp}")
            for o in result.orders
        ]

    def _exit_owner(self, positions: Mapping[str, float]) -> str | None:
        """The strategy behind the most recent fill on a held ticker."""
        owners = [self._fill_owner[t] for t, q in positions.items() if q and t in self._fill_owner]
        return max(owners)[1] if owners else None

    def _load_history(self, bars_by_ts: Mapping[datetime, object]) -> dict[str, pd.DataFrame]:
        """Daily adjusted history for the pipeline's volatilities and risk
        context: ``history_bars`` before the window plus the window itself.
        Only loaded when the pipeline runs."""
        if self._config.construction_settings is None or not bars_by_ts:
            return {}
        from stonks.production.prices import load_history

        end = max(bars_by_ts).date()
        days = len({ts.date() for ts in bars_by_ts})
        history = load_history(
            self._lake,
            list(self._config.universe),
            end,
            bars=self._config.history_bars + days,
        )
        self._raw_closes = self._load_raw_closes(end)
        return history

    def _load_raw_closes(self, end: date) -> dict[str, dict[date, float]]:
        """Unadjusted daily closes up to ``end``, per ticker, for rebasing
        the pipeline history (see the module doc)."""
        frame = self._lake.sql(
            "SELECT ticker, timestamp, close FROM bars"
            " WHERE ticker = ANY(?) AND interval = '1d' AND timestamp < ?",
            [list(self._config.universe), day_start(end + timedelta(days=1))],
        )
        out: dict[str, dict[date, float]] = {}
        for row in frame.itertuples(index=False):
            if row.close is not None and not pd.isna(row.close) and row.close > 0:
                out.setdefault(str(row.ticker), {})[_as_date(row.timestamp)] = float(row.close)
        return out

    def _history_until(self, as_of: datetime) -> dict[str, pd.DataFrame]:
        """History a decision at ``as_of`` may see: daily bars dated on or
        before its day for a daily run (the bar closed at the decision),
        strictly before it for intraday runs (today's daily bar is not
        complete yet)."""
        last = as_of.date()
        if self._config.interval != Interval.DAY_1:
            last -= timedelta(days=1)
        cutoff = pd.Timestamp(last)
        out: dict[str, pd.DataFrame] = {}
        for ticker, frame in self._history.items():
            visible = frame.loc[:cutoff].tail(self._config.history_bars)
            if not visible.empty:
                out[ticker] = self._rebased(ticker, visible)
        return out

    def _rebased(self, ticker: str, frame: pd.DataFrame) -> pd.DataFrame:
        """``frame`` scaled so its last close is that day's raw close: any
        split or dividend after the slice drops out (RS-15)."""
        raw = self._raw_closes.get(ticker, {}).get(_as_date(frame.index[-1]))
        last = frame["close"].iloc[-1] if "close" in frame else None
        if raw is None or last is None or pd.isna(last) or last <= 0:
            return frame
        factor = raw / float(last)
        if abs(factor - 1.0) <= 1e-12:
            return frame
        out = frame.copy()
        for col in ("open", "high", "low", "close", "adj_close"):
            if col in out:
                out[col] = out[col] * factor
        if "volume" in out:
            out["volume"] = out["volume"] / factor
        return out


_BAR_COLUMNS = """b.timestamp, b.ticker, b.open, b.high, b.low, b.close, b.adj_close,
                   b.volume, COALESCE(i.asset_class, 'equity') AS asset_class"""


def _float(value) -> float | None:
    return None if pd.isna(value) else float(value)


def _to_window_bounds(start, end) -> tuple[datetime, datetime]:
    return day_start(start), day_end(end)


def _as_date(value) -> date:
    """A membership date from DuckDB / pandas as a plain ``date``."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return pd.Timestamp(value).date()

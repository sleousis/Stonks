"""The decision step: decide on every bar close (roadmap 21.2.2).

:class:`DecisionStep` is an :class:`~stonks.engine.driver.EventDriver`
handler. On each :class:`~stonks.engine.driver.BarClose` it:

1. updates the marks (the last close per ticker, carried forward);
2. opens a point-in-time view of the lake for the bar that just closed
   (:class:`~stonks.store.pit.PointInTimeLake`, decision interval = the
   driver's interval), so a strategy sees only bars closed by the event
   time (P12);
3. scores every strategy on every universe ticker once
   (``estimate_return``), shared by all books;
4. runs :func:`stonks.portfolio.pipeline.build_orders` per book, the one
   pipeline the daily tick and the bar backtester share;
5. gates the orders with the book's session rules
   (:mod:`stonks.engine.sessions`): no entries at the edges, nothing in a
   halted or closed ticker, and every position closed in the flatten
   window. A book without session rules is not gated (research parity);
6. hands one :class:`StepDecision` per book to ``on_decision`` (the
   intraday backtest's fills, the router of 21.2.3).

Time: strategies get ``as_of`` = the start of the bar that just closed, in
naive UTC, the same ``as_of`` the bar backtester passes. The session rules
and ``Order.decided_at`` use the event's close time (aware UTC), the same
instant in a replay and live.

The step never places an order and never writes the lake. Fills come back
through :meth:`DecisionStep.record_fill`, so the owner of a holding can
still exit it when nothing is picked.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pandas as pd

from stonks.core.interval import Interval
from stonks.core.protocols import Strategy
from stonks.core.types import Order, Portfolio
from stonks.engine.driver import BarClose
from stonks.engine.sessions import (
    NO_HALTS,
    DroppedOrder,
    HaltTable,
    SessionRulebook,
    SessionRules,
    flatten_orders,
    gate_orders,
)
from stonks.execution.orders import SideToken
from stonks.logging import get_logger
from stonks.portfolio.settings import ConstructionSettings
from stonks.store.pit import PitSession, PointInTimeLake

if TYPE_CHECKING:
    # The pipeline imports ``stonks.config``, which reaches the lab and the
    # backtest: imported where it runs.
    from stonks.config import RiskPolicy
    from stonks.portfolio.pipeline import FillCosts, PipelineResult

_log = get_logger("stonks.engine.step")

_HISTORY_COLUMNS = ["open", "high", "low", "close", "volume"]


@dataclass(frozen=True, kw_only=True)
class StepBook:
    """One book the step decides for."""

    id: str
    #: Stamped on the orders and in their client ids (``None``: the only book).
    portfolio_id: str | None = None
    construction: ConstructionSettings = field(default_factory=ConstructionSettings)
    risk: RiskPolicy | None = None
    strategy_weights: Mapping[str, float] | None = None
    risk_overrides: Mapping[str, RiskPolicy] = field(default_factory=dict)
    costs: FillCosts | None = None
    allow_short: bool = False
    #: ``None``: no session gate (a research run, parity with the bar backtester).
    sessions: SessionRules | None = None


@dataclass(frozen=True)
class StepDecision:
    """What one book decided on one bar close."""

    book_id: str
    #: The bar close (aware UTC): the decision instant.
    at: datetime
    #: The start of the decision bar (naive UTC), the ``as_of`` strategies saw.
    as_of: datetime
    orders: list[Order]
    dropped: list[DroppedOrder] = field(default_factory=list)
    #: Tickers closed in full by the flatten window.
    flattened: list[str] = field(default_factory=list)
    result: PipelineResult | None = None


class DecisionStep:
    """See the module doc. ``portfolio(book_id)`` returns the book's current
    portfolio. ``stale_after`` stops opening orders on a ticker whose last
    bar is older than that at the decision (``None``: no check, as in the bar
    backtester). ``fresh_reads`` opens a new point-in-time session per bar
    close, for a live lake that grows while the step runs; a replay reads
    each history once."""

    name = "decision_step"

    def __init__(
        self,
        strategies: Mapping[str, Strategy],
        books: Sequence[StepBook],
        lake: Any,
        *,
        universe: Sequence[str],
        portfolio: Callable[[str], Portfolio],
        interval: Interval = Interval.MIN_1,
        on_decision: Callable[[StepDecision], object] | None = None,
        threshold: float = 0.0,
        asset_classes: Mapping[str, str] | None = None,
        halts: HaltTable = NO_HALTS,
        stale_after: timedelta | None = None,
        history_bars: int = 260,
        fresh_reads: bool = False,
    ) -> None:
        if not strategies:
            raise ValueError("the decision step needs at least one strategy")
        if not books:
            raise ValueError("the decision step needs at least one book")
        ids = [b.id for b in books]
        if len(set(ids)) != len(ids):
            raise ValueError(f"book ids must be unique, got {ids}")
        self.strategies = dict(strategies)
        self.books = list(books)
        self.lake = lake
        self.universe = list(dict.fromkeys(universe))
        self.interval = interval
        self.threshold = threshold
        self.halts = halts
        self.stale_after = stale_after
        self.history_bars = history_bars
        self.fresh_reads = fresh_reads
        self._portfolio = portfolio
        self._on_decision = on_decision
        self._asset_classes = dict(asset_classes) if asset_classes is not None else None
        self._pit = PitSession(lake)
        self._marks: dict[str, float] = {}
        self._last_bar: dict[str, datetime] = {}
        self._attribution: dict[str, dict[str, dict[str, float]]] = {b.id: {} for b in books}
        self._fill_owner: dict[str, dict[str, tuple[int, str]]] = {b.id: {} for b in books}
        self._fill_seq = 0

    # ---- state -----------------------------------------------------------------------

    @property
    def marks(self) -> dict[str, float]:
        """The last close per ticker (a copy)."""
        return dict(self._marks)

    @property
    def asset_classes(self) -> dict[str, str]:
        if self._asset_classes is None:
            reader: Any = getattr(self.lake, "get_asset_classes", None)
            found: Any = reader(self.universe) if callable(reader) else {}
            self._asset_classes = {t: str(found.get(t) or "equity") for t in self.universe}
        return dict(self._asset_classes)

    def record_fill(self, book_id: str, ticker: str, strategy_id: str | None) -> None:
        """A fill of ``book_id`` in ``ticker`` for ``strategy_id``: the most
        recent filler owns the holding's exit when nothing is picked."""
        if strategy_id is None:
            return
        self._fill_seq += 1
        self._fill_owner[book_id][ticker] = (self._fill_seq, strategy_id)

    # ---- the handler -----------------------------------------------------------------

    def on_bar_close(self, event: BarClose) -> list[StepDecision]:
        if event.interval != self.interval:
            return []
        at = event.at.astimezone(UTC)
        for bar in event.bars:
            self._marks[bar.ticker] = bar.close
            self._last_bar[bar.ticker] = at
        as_of = (at - self.interval.to_timedelta()).replace(tzinfo=None)
        if self.fresh_reads:
            self._pit = PitSession(self.lake)
        view = self._pit.at(as_of, decision_interval=self.interval)
        from stonks.strategies._common import decision_interval

        decisions: list[StepDecision] = []
        with decision_interval(self.interval):
            signals = self._signals(as_of, view)
            for book in self.books:
                decision = self._decide_book(book, signals, at, as_of, view)
                decisions.append(decision)
                if self._on_decision is not None:
                    self._on_decision(decision)
        return decisions

    # ---- internals -------------------------------------------------------------------

    def _signals(self, as_of: datetime, view: PointInTimeLake) -> dict[str, dict[str, float]]:
        """Every strategy's scores above the threshold (below minus it too
        for a strategy that may short), once per bar for all books."""
        out: dict[str, dict[str, float]] = {}
        for sid, strategy in self.strategies.items():
            shorts = bool(getattr(strategy, "supports_short", False))
            scores: dict[str, float] = {}
            for ticker in self.universe:
                r = strategy.estimate_return(ticker, as_of, view)  # type: ignore[arg-type]
                if r is None or not math.isfinite(r):
                    continue
                if r > self.threshold or (shorts and r < -self.threshold):
                    scores[ticker] = float(r)
            out[sid] = scores
        return out

    def _decide_book(
        self,
        book: StepBook,
        signals: Mapping[str, Mapping[str, float]],
        at: datetime,
        as_of: datetime,
        view: PointInTimeLake,
    ) -> StepDecision:
        from stonks.portfolio.pipeline import (
            PORTFOLIO_STRATEGY,
            BookInput,
            FillCosts,
            build_orders,
        )

        portfolio = self._portfolio(book.id)
        book_signals = {
            sid: dict(scores)
            if book.allow_short
            else {t: r for t, r in scores.items() if r > self.threshold}
            for sid, scores in signals.items()
        }
        market = self._market(book, as_of, at, view, portfolio, book_signals)
        inputs = BookInput(
            portfolio=portfolio,
            construction=book.construction,
            risk=book.risk,
            strategy_weights=book.strategy_weights,
            risk_overrides=book.risk_overrides,
            prior_attribution=self._attribution[book.id],
            costs=book.costs or FillCosts(),
            risk_context=self._risk_context(book, portfolio, market, as_of),
            allow_short=book.allow_short,
        )
        prefix = f"{book.portfolio_id}:" if book.portfolio_id else ""
        stamp = as_of.isoformat()

        def client_id(strategy_id: str | None, ticker: str, side: SideToken) -> str:
            return f"{prefix}{strategy_id or PORTFOLIO_STRATEGY}:{stamp}:{ticker}:{side}"

        result = build_orders(
            book_signals,
            inputs,
            market,
            strategies=lambda sid: self.strategies[sid],  # type: ignore[arg-type, return-value]
            exit_owner=lambda: self._exit_owner(book.id, portfolio.positions),
            client_id=client_id,
        )
        self._attribution[book.id] = {**self._attribution[book.id], **result.attribution}
        orders = [
            o if stamp in o.client_id else replace(o, client_id=f"{o.client_id}@{stamp}")
            for o in result.orders
        ]
        orders = [
            replace(
                o,
                portfolio_id=book.portfolio_id,
                decided_at=at,
                decision_price=self._marks.get(o.ticker),
            )
            for o in orders
        ]
        dropped: list[DroppedOrder] = []
        flattened: list[str] = []
        if book.sessions is not None:
            orders, dropped, flattened = self._gate(book, book.sessions, orders, portfolio, at)
        return StepDecision(book.id, at, as_of, orders, dropped, flattened, result)

    def _gate(
        self,
        book: StepBook,
        rules: SessionRules,
        orders: list[Order],
        portfolio: Portfolio,
        at: datetime,
    ) -> tuple[list[Order], list[DroppedOrder], list[str]]:
        rulebook = SessionRulebook(rules, halts=self.halts)
        classes = self.asset_classes

        def state_for(ticker: str):
            return rulebook.state(at, ticker, asset_class=classes.get(ticker))

        kept, dropped = gate_orders(orders, portfolio.positions, state_for)
        flat = flatten_orders(
            portfolio.positions, state_for, portfolio_id=book.portfolio_id, prices=self._marks
        )
        if not flat:
            return kept, dropped, []
        closing = {o.ticker for o in flat}
        kept = [o for o in kept if o.ticker not in closing]
        flat = [replace(o, decision_price=self._marks.get(o.ticker)) for o in flat]
        return kept + flat, dropped, sorted(closing)

    def _market(
        self,
        book: StepBook,
        as_of: datetime,
        at: datetime,
        view: PointInTimeLake,
        portfolio: Portfolio,
        signals: Mapping[str, Mapping[str, float]],
    ):
        from stonks.portfolio import returns as portfolio_returns
        from stonks.portfolio.pipeline import MarketView, vols_from_history

        buyable = None
        if self.stale_after is not None:
            buyable = [t for t, seen in self._last_bar.items() if at - seen <= self.stale_after]
        market = MarketView(
            as_of=as_of,
            prices=dict(self._marks),
            buyable=buyable,
            asset_classes=self.asset_classes,
        )
        if book.construction.is_single_winner:
            return market
        names = {t for scores in signals.values() for t in scores} | set(portfolio.positions)
        market = replace(market, vols_annual=vols_from_history(self._daily(view, names)))
        lookback = portfolio_returns.returns_lookback(book.construction)
        if lookback is not None:
            past = portfolio_returns.market_history(view, names, lookback=lookback)
            market = replace(market, returns_history=past.returns, volumes=past.volumes)
        return market

    def _risk_context(self, book: StepBook, portfolio: Portfolio, market: Any, as_of: datetime):
        if book.risk is None:
            return None
        from stonks.production.rules import RiskContext

        return RiskContext(
            portfolio=portfolio,
            prices=market.prices,
            asset_classes=market.asset_classes,
            policy=book.risk,
            as_of=as_of.date(),
            portfolio_id=book.portfolio_id,
            allow_short=book.allow_short,
        )

    def _daily(self, view: PointInTimeLake, tickers: set[str]) -> dict[str, pd.DataFrame]:
        """Adjusted daily bars known at the decision, date indexed (the
        shape ``production.prices.load_history`` returns)."""
        out: dict[str, pd.DataFrame] = {}
        for ticker in sorted(tickers):
            bars = view.get_bars(ticker, Interval.DAY_1, None, None)
            if bars is None or bars.empty:
                continue
            bars = bars.tail(self.history_bars).copy()
            close = bars["close"].astype(float)
            adj = bars["adj_close"].astype(float) if "adj_close" in bars else close
            factor = (adj / close).where(close > 0).fillna(1.0)
            for col in ("open", "high", "low", "close"):
                bars[col] = bars[col].astype(float) * factor
            bars.index = pd.DatetimeIndex(pd.to_datetime(bars["timestamp"]).dt.normalize())
            out[ticker] = bars[_HISTORY_COLUMNS].rename_axis("date")
        return out

    def _exit_owner(self, book_id: str, positions: Mapping[str, float]) -> str | None:
        owners = self._fill_owner[book_id]
        held = [owners[t] for t, q in positions.items() if q and t in owners]
        return max(held)[1] if held else None

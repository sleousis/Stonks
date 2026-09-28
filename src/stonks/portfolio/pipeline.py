"""The one construction pipeline, shared by the tick and the backtest (BL-12).

:func:`build_orders` turns one decision date's raw strategy signals into the
orders of one book:

1. keep the signals of the book's strategies (``strategy_weights``; ``None``
   means every strategy that sent signals);
2. normalise them with the constructor's ``signal_method``;
3. run the registered :class:`~stonks.portfolio.base.PortfolioConstructor`
   (``[production.construction].method``, per portfolio);
4. turn the target book into orders;
5. drop opening orders of tickers without a fresh price, then apply the risk rules.

Step 4 has two routes:

- ``single_winner`` (the default, today's behaviour kept exactly): the
  strategy owning the best pick takes the book, and its own ``decide`` makes
  the orders from its picks. With nothing picked, the owner of the holdings
  (``exit_owner``) decides with no picks, so its exits still happen.
- every other constructor: the target weights are diffed against the book
  with the no-trade buffer (:func:`~stonks.portfolio.orders.orders_from_targets`).
  A held ticker no strategy targets any more is sold, so exits need no owner.

The result carries the target book and, per ticker, each strategy's share of
it (``attribution``), so P&L can be attributed per strategy per portfolio.
An order belongs to the strategy with the largest share of its ticker (for a
full exit, the largest share recorded before, ``prior_attribution``); an
order no strategy owns is the portfolio's own (``strategy_id=None``,
client-id segment :data:`PORTFOLIO_STRATEGY`).

The pipeline is pure: no database, no lake. Callers bring the market view
(prices, fresh tickers, volumes, volatilities) and the strategies.

Shorts (roadmap 16.1)
---------------------
A short opens only when the book allows it (``BookInput.allow_short``) and
the strategy does (``supports_short``, read with ``getattr``). Otherwise
everything is exactly as above. In a short book the target-weight route
keeps negative scores of the strategies that support shorts (the others
are clipped at 0), lets the constructor keep negative weights (its
long/short mode, roadmap 16.3: its own normalisation, gross and net
limits, neutrality), and diffs signed targets
(``orders_from_targets(allow_short=True)``). A book that cannot short runs
its constructor long-only at no more than 1.0 gross, whatever its
settings say. In
``single_winner`` the winner's orders are split at zero
(:func:`stonks.execution.orders.classify`) and the opening sell legs are
dropped unless the winner supports shorts. Client ids carry the side token
(``short``, ``cover``) so the two legs of a split stay distinct.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from typing import Any, Literal, Protocol

import numpy as np
import pandas as pd

from stonks.backtest.costs import CostModel, CostModelSettings
from stonks.config import RiskPolicy
from stonks.core.types import Order, Portfolio
from stonks.execution.orders import SideToken, classify_all, make_client_id, side_token
from stonks.features.volatility import annualize, ewma_vol
from stonks.logging import get_logger
from stonks.portfolio.base import (
    ConstructionInput,
    PortfolioConstructor,
    TargetBook,
)
from stonks.portfolio.orders import orders_from_targets
from stonks.portfolio.settings import ConstructionSettings
from stonks.portfolio.signals import SignalContext, normalize
from stonks.production.prices import drop_stale_opens
from stonks.production.risk import RiskAdjustment, RiskContext, RiskResult, apply_risk

_log = get_logger("stonks.portfolio.pipeline")

#: Client-id segment of an order no single strategy owns.
PORTFOLIO_STRATEGY = "portfolio"

NoTradeReason = Literal["no_candidates", "no_active_owner", "no_signals"]


@dataclass(frozen=True)
class FillCosts:
    """How the risk layer prices its cash estimate: a cost model, or the
    legacy flat slippage and fee (exclusive)."""

    slippage_bps: float = 0.0
    fee_per_trade: float = 0.0
    model: CostModel | CostModelSettings | None = None


@dataclass(frozen=True, kw_only=True)
class MarketView:
    """Market data of one decision date, shared by every book.

    ``buyable``: tickers with a fresh price (``None`` = every priced one).
    ``vols_annual`` and ``returns_history`` feed the volatility-aware
    constructors (the history from :mod:`stonks.portfolio.returns`); nothing
    in them may be dated after ``as_of``."""

    as_of: date | datetime
    prices: Mapping[str, float]
    buyable: Collection[str] | None = None
    volumes: Mapping[str, float] | None = None
    asset_classes: Mapping[str, str] = field(default_factory=dict)
    vols_annual: Mapping[str, float] = field(default_factory=dict)
    returns_history: pd.DataFrame | None = None
    #: Raw style exposures known at ``as_of``, rows tickers (roadmap 22.4).
    factor_exposures: pd.DataFrame | None = None


@dataclass(frozen=True, kw_only=True)
class BookInput:
    """One book as the pipeline sees it.

    ``risk=None`` skips the risk layer (a backtest without a policy).
    ``risk_overrides``: a tighter policy per strategy slice, used when that
    strategy decides alone (``single_winner``). ``prior_attribution``: the
    last recorded strategy shares per held ticker."""

    portfolio: Portfolio
    construction: ConstructionSettings = field(default_factory=ConstructionSettings)
    risk: RiskPolicy | None = None
    strategy_weights: Mapping[str, float] | None = None
    risk_overrides: Mapping[str, RiskPolicy] = field(default_factory=dict)
    prior_attribution: Mapping[str, Mapping[str, float]] = field(default_factory=dict)
    costs: FillCosts = field(default_factory=FillCosts)
    risk_context: RiskContext | None = None
    #: The book may hold short positions (``portfolios.allow_short``).
    allow_short: bool = False

    def strategies(self, signals: Mapping[str, Any]) -> list[str]:
        """The book's strategies that sent signals, in signal order."""
        if self.strategy_weights is None:
            return list(signals)
        return [s for s in signals if float(self.strategy_weights.get(s, 0.0)) > 0.0]


class Decider(Protocol):
    def decide(
        self,
        my_picks: Sequence[tuple[float, str]],
        portfolio: Portfolio,
        prices: Mapping[str, float],
        as_of: Any,
    ) -> list[Order]: ...


#: ``(strategy_id or None, ticker, side token) -> client id``.
ClientIdFn = Callable[[str | None, str, SideToken], str]


@dataclass(frozen=True)
class PipelineResult:
    orders: list[Order]
    target_book: TargetBook
    adjustments: list[RiskAdjustment] = field(default_factory=list)
    #: Tickers whose opening orders were dropped for a stale price.
    stale_buys: list[str] = field(default_factory=list)
    #: ``single_winner``: the strategy whose ``decide`` made the orders.
    decided_by: str | None = None
    winner_return: float | None = None
    #: The owner of the holdings decided without picks.
    exit_only: bool = False
    #: Why nothing was decided (no orders at all).
    reason: NoTradeReason | None = None
    #: Per ticker, each strategy's share of what this decision targeted.
    attribution: dict[str, dict[str, float]] = field(default_factory=dict)
    #: The orders before the stale-price guard and the risk rules (23.7).
    proposed: list[Order] = field(default_factory=list)


def build_orders(
    signals: Mapping[str, Mapping[str, float]],
    book: BookInput,
    market: MarketView,
    *,
    strategies: Callable[[str], Decider] | None = None,
    exit_owner: Callable[[], str | None] | None = None,
    client_id: ClientIdFn | None = None,
) -> PipelineResult:
    """See the module doc. ``strategies(sid)`` returns the strategy that
    decides in the ``single_winner`` route; ``exit_owner()`` names the owner
    of the holdings when nothing was picked (``None``: nobody active)."""
    ids = book.strategies(signals)
    book_signals = {sid: dict(signals[sid]) for sid in ids}
    make_id = client_id or _default_client_id(market.as_of)
    constructor = book.construction.build()
    if book.construction.is_single_winner:
        return _single_winner(
            constructor, book_signals, book, market, strategies, exit_owner, make_id
        )
    return _from_targets(constructor, book_signals, book, market, make_id, strategies)


def apply_book_risk(
    orders: Sequence[Order],
    book: BookInput,
    market: MarketView,
    policy: RiskPolicy | None = None,
) -> RiskResult:
    """The risk layer as the pipeline runs it (``book.risk`` unless a slice
    ``policy`` is given); the tick re-checks its buys after the sells with
    this. ``None`` everywhere passes the orders through."""
    policy = policy if policy is not None else book.risk
    if policy is None:
        return RiskResult(orders=list(orders), adjustments=[])
    return apply_risk(
        orders,
        book.portfolio,
        market.prices,
        market.asset_classes,
        policy,
        slippage_bps=book.costs.slippage_bps,
        fee_per_trade=book.costs.fee_per_trade,
        cost_model=book.costs.model,
        volumes=market.volumes,
        context=book.risk_context,
        allow_short=book.allow_short,
    )


def vols_from_history(
    history: Mapping[str, pd.DataFrame],
    *,
    span: int = 35,
    periods_per_year: float = 252.0,
) -> dict[str, float]:
    """Annualised EWMA sigma of daily close-to-close returns per ticker,
    from each frame's last row (tickers without enough bars are left out).
    Callers pass history ending at the decision date, never later."""
    out: dict[str, float] = {}
    for ticker, frame in history.items():
        if frame is None or "close" not in frame or len(frame) <= span:
            continue
        returns = np.log(frame["close"].astype(float)).diff()
        sigma = ewma_vol(returns, span=span).iloc[-1]
        if sigma is not None and math.isfinite(sigma) and sigma > 0:
            out[str(ticker)] = float(annualize(sigma, periods_per_year))
    return out


# ---- routes ----------------------------------------------------------------------


def _single_winner(
    constructor: PortfolioConstructor,
    signals: dict[str, dict[str, float]],
    book: BookInput,
    market: MarketView,
    strategies: Callable[[str], Decider] | None,
    exit_owner: Callable[[], str | None] | None,
    make_id: ClientIdFn,
) -> PipelineResult:
    target = constructor.target_weights(_construction_input(signals, book, market))
    winner: str | None = target.meta.get("winner_strategy_id")
    picks: list[tuple[float, str]] = list(target.meta.get("picks", []))
    exit_only = False
    if winner is None:
        if not _held(book.portfolio):
            return PipelineResult(orders=[], target_book=target, reason="no_candidates")
        winner = exit_owner() if exit_owner is not None else None
        if winner is None:
            return PipelineResult(orders=[], target_book=target, reason="no_active_owner")
        exit_only, picks = True, []
    if strategies is None:
        raise ValueError("the single_winner route needs the strategies that decide")
    decider = strategies(winner)
    proposed = decider.decide(picks, book.portfolio, dict(market.prices), market.as_of)
    if book.allow_short:
        proposed = _split_at_zero(proposed, book.portfolio, _supports_short(decider))
    kept, stale = _drop_stale(proposed, market, book.portfolio)
    risk = apply_book_risk(kept, book, market, book.risk_overrides.get(winner))
    # Orders a risk rule created (e.g. a max_holding forced sell) keep the
    # rule's client id and no strategy; the winner's own orders take its ids.
    decided = {o.client_id for o in kept}
    orders = [
        replace(o, strategy_id=winner, client_id=make_id(winner, o.ticker, side_token(o)))
        if o.client_id in decided
        else o
        for o in risk.orders
    ]
    return PipelineResult(
        orders=orders,
        target_book=target,
        adjustments=list(risk.adjustments),
        stale_buys=stale,
        decided_by=winner,
        winner_return=None if exit_only else picks[0][0],
        exit_only=exit_only,
        proposed=list(proposed),
        # every opening order, long or short, is the winner's share (BE-50)
        attribution={o.ticker: {winner: 1.0} for o in orders if _opens(o, book.portfolio)},
    )


def _from_targets(
    constructor: PortfolioConstructor,
    signals: dict[str, dict[str, float]],
    book: BookInput,
    market: MarketView,
    make_id: ClientIdFn,
    strategies: Callable[[str], Decider] | None = None,
) -> PipelineResult:
    if not signals:
        # Not one of the book's strategies was scored (failed to load, not
        # subscribed): hold rather than read it as "exit everything".
        _log.warning("pipeline.no_signals", held=sorted(_held(book.portfolio)))
        return PipelineResult(orders=[], target_book=TargetBook(), reason="no_signals")
    shorting = book.allow_short and _any_shorts(signals, strategies)
    if shorting:
        constructor = _long_short(book.construction)
    elif not constructor.settings.long_only:
        constructor = _long_only(book.construction)
    method = book.construction.signal_method or constructor.normalization()
    normalised = normalize(
        signals,
        method,
        long_only=constructor.settings.long_only,
        context=SignalContext(vols_annual=market.vols_annual),
    )
    if shorting:
        normalised = {
            sid: scores
            if _supports_short(strategies(sid) if strategies else None)
            else {t: max(v, 0.0) for t, v in scores.items()}
            for sid, scores in normalised.items()
        }
    target = constructor.target_weights(_construction_input(normalised, book, market))
    decision_date = market.as_of.date() if isinstance(market.as_of, datetime) else market.as_of
    raw = orders_from_targets(
        target.weights,
        book.portfolio,
        market.prices,
        buffer_fraction=book.construction.buffer_fraction,
        min_trade_weight=book.construction.min_trade_weight,
        as_of=decision_date,
        allow_short=shorting,
    )
    owned = []
    for order in raw:
        owner = _dominant(target.attribution.get(order.ticker)) or _dominant(
            book.prior_attribution.get(order.ticker)
        )
        owned.append(
            replace(
                order, strategy_id=owner, client_id=make_id(owner, order.ticker, side_token(order))
            )
        )
    kept, stale = _drop_stale(owned, market, book.portfolio)
    risk = apply_book_risk(kept, book, market)
    return PipelineResult(
        orders=list(risk.orders),
        target_book=target,
        adjustments=list(risk.adjustments),
        stale_buys=stale,
        attribution={t: dict(s) for t, s in target.attribution.items() if t in target.weights},
        proposed=owned,
    )


# ---- helpers ---------------------------------------------------------------------


def _construction_input(
    signals: Mapping[str, Mapping[str, float]], book: BookInput, market: MarketView
) -> ConstructionInput:
    weights = None
    if book.strategy_weights is not None:
        weights = {sid: float(book.strategy_weights[sid]) for sid in signals}
    decision_date = market.as_of.date() if isinstance(market.as_of, datetime) else market.as_of
    return ConstructionInput(
        signals=signals,
        portfolio=book.portfolio,
        prices=market.prices,
        as_of=decision_date,
        strategy_weights=weights,
        vols_annual=market.vols_annual,
        asset_classes=market.asset_classes,  # type: ignore[arg-type]
        returns_history=market.returns_history,
        volumes=market.volumes or {},
        factor_exposures=market.factor_exposures,
    )


def _supports_short(strategy: object) -> bool:
    return bool(getattr(strategy, "supports_short", False))


def _any_shorts(
    signals: Mapping[str, Mapping[str, float]], strategies: Callable[[str], Decider] | None
) -> bool:
    """Any of the book's signalling strategies may short."""
    return strategies is not None and any(_supports_short(strategies(s)) for s in signals)


def _long_short(construction: ConstructionSettings) -> PortfolioConstructor:
    """The book's constructor with negative weights allowed."""
    params = {**construction.params, "long_only": False}
    return construction.model_copy(update={"params": params}).build()


def _long_only(construction: ConstructionSettings) -> PortfolioConstructor:
    """The book's constructor as a cash book: long-only, at most 1.0 gross,
    no neutrality (a book that cannot short, or has no strategy that may)."""
    params = {
        **construction.params,
        "long_only": True,
        "max_gross": min(float(construction.params.get("max_gross", 1.0)), 1.0),
        "neutral": "none",
    }
    return construction.model_copy(update={"params": params}).build()


def _split_at_zero(orders: Sequence[Order], portfolio: Portfolio, may_short: bool) -> list[Order]:
    """Orders split at zero against the book; opening sells are dropped
    unless the strategy may short. A leg whose token matches the order's
    side keeps its client id."""
    out: list[Order] = []
    for leg in classify_all(orders, portfolio.positions):
        if leg.side == "sell" and leg.position_effect == "open" and not may_short:
            _log.info("pipeline.short_dropped", ticker=leg.ticker, quantity=leg.quantity)
            continue
        out.append(leg)
    return out


def _opens(order: Order, portfolio: Portfolio) -> bool:
    if order.position_effect is not None:
        return order.position_effect == "open"
    held = portfolio.positions.get(order.ticker, 0.0)
    return held >= 0 if order.side == "buy" else held <= 0


def _drop_stale(
    orders: Sequence[Order], market: MarketView, portfolio: Portfolio
) -> tuple[list[Order], list[str]]:
    """Opening orders on tickers without a fresh price are dropped; closes
    always go (BE-11)."""
    if market.buyable is None:
        return list(orders), []
    return drop_stale_opens(orders, market.buyable, portfolio.positions)


def _held(portfolio: Portfolio) -> list[str]:
    return [t for t, q in portfolio.positions.items() if abs(q) > 1e-12]


def _dominant(shares: Mapping[str, float] | None) -> str | None:
    """The strategy with the largest share (ties: lowest id)."""
    if not shares:
        return None
    return min(shares, key=lambda sid: (-abs(shares[sid]), sid))


def _default_client_id(as_of: date | datetime) -> ClientIdFn:
    def make(strategy_id: str | None, ticker: str, side: SideToken) -> str:
        return make_client_id(
            as_of=as_of,  # type: ignore[arg-type]
            strategy_id=strategy_id or PORTFOLIO_STRATEGY,
            ticker=ticker,
            side=side,
        )

    return make

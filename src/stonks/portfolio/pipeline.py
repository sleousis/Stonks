"""The one construction pipeline, shared by the tick and the backtest (BL-12).

:func:`build_orders` turns one decision date's raw strategy signals into the
orders of one book:

1. keep the signals of the book's strategies (``strategy_weights``; ``None``
   means every strategy that sent signals);
2. normalise them with the constructor's ``signal_method``;
3. run the registered :class:`~stonks.portfolio.base.PortfolioConstructor`
   (``[production.construction].method``, per portfolio);
4. turn the target book into orders;
5. drop buys of tickers without a fresh price, then apply the risk rules.

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
"""

from __future__ import annotations

import math
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from typing import Any, Literal, Protocol

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, model_validator

from stonks.backtest.costs import CostModel, CostModelSettings
from stonks.config import RiskPolicy
from stonks.core.types import Order, OrderSide, Portfolio
from stonks.execution.orders import make_client_id
from stonks.features.volatility import annualize, ewma_vol
from stonks.logging import get_logger
from stonks.portfolio.base import (
    ConstructionInput,
    PortfolioConstructor,
    TargetBook,
    get_constructor,
)
from stonks.portfolio.orders import orders_from_targets
from stonks.portfolio.signals import SignalContext, normalize
from stonks.production.prices import drop_stale_buys
from stonks.production.risk import RiskAdjustment, RiskContext, apply_risk

_log = get_logger("stonks.portfolio.pipeline")

SINGLE_WINNER = "single_winner"
#: Client-id segment of an order no single strategy owns.
PORTFOLIO_STRATEGY = "portfolio"
#: Keys of a construction mapping that configure the pipeline itself; every
#: other key is a knob of the constructor.
_PIPELINE_KEYS = frozenset({"method", "signal_method", "buffer_fraction", "min_trade_weight"})

NoTradeReason = Literal["no_candidates", "no_active_owner", "no_signals"]


class ConstructionSettings(BaseModel):
    """``[production.construction]``, merged per portfolio: which
    constructor sizes the book and how its targets become orders.

    ``params`` are the constructor's own knobs (validated by its
    ``Settings``); ``signal_method`` overrides the constructor's
    normalisation. ``buffer_fraction`` and ``min_trade_weight`` are the
    no-trade buffer of the target-weight route (``single_winner`` trades
    through the strategy's ``decide`` and ignores them)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    method: str = SINGLE_WINNER
    params: dict[str, Any] = Field(default_factory=dict)
    signal_method: str | None = None
    buffer_fraction: float = Field(default=0.10, ge=0.0, le=0.5)
    min_trade_weight: float = Field(default=0.005, ge=0.0)

    @model_validator(mode="after")
    def _known_constructor(self) -> ConstructionSettings:
        self.build()  # unknown names and bad knobs fail at load time
        return self

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any] | None) -> ConstructionSettings:
        """From a flat mapping (a portfolio's ``construction_json`` merged
        with the global settings): pipeline keys are fields, the rest are
        the constructor's ``params``."""
        flat = dict(mapping or {})
        params = dict(flat.pop("params", None) or {})
        params.update({k: v for k, v in flat.items() if k not in _PIPELINE_KEYS})
        return cls(**{k: v for k, v in flat.items() if k in _PIPELINE_KEYS}, params=params)

    @property
    def is_single_winner(self) -> bool:
        return self.method == SINGLE_WINNER

    def build(self) -> PortfolioConstructor:
        params = dict(self.params)
        if self.is_single_winner:
            # Signals arrive already filtered by the signal phase's threshold.
            params.setdefault("threshold", -math.inf)
        return get_constructor(self.method, **params)


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
    constructors; nothing in them may be dated after ``as_of``."""

    as_of: date | datetime
    prices: Mapping[str, float]
    buyable: Collection[str] | None = None
    volumes: Mapping[str, float] | None = None
    asset_classes: Mapping[str, str] = field(default_factory=dict)
    vols_annual: Mapping[str, float] = field(default_factory=dict)
    returns_history: pd.DataFrame | None = None


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


#: ``(strategy_id or None, ticker, side) -> client id``.
ClientIdFn = Callable[[str | None, str, OrderSide], str]


@dataclass(frozen=True)
class PipelineResult:
    orders: list[Order]
    target_book: TargetBook
    adjustments: list[RiskAdjustment] = field(default_factory=list)
    #: Tickers whose buys were dropped for a stale price.
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
    return _from_targets(constructor, book_signals, book, market, make_id)


def apply_book_risk(
    orders: Sequence[Order],
    book: BookInput,
    market: MarketView,
    policy: RiskPolicy | None = None,
):
    """The risk layer as the pipeline runs it (``book.risk`` unless a slice
    ``policy`` is given); the tick re-checks its buys after the sells with
    this. ``None`` everywhere passes the orders through."""
    from stonks.production.risk import RiskResult

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
    proposed = strategies(winner).decide(picks, book.portfolio, dict(market.prices), market.as_of)
    kept, stale = _drop_stale(proposed, market)
    risk = apply_book_risk(kept, book, market, book.risk_overrides.get(winner))
    orders = [
        replace(o, strategy_id=winner, client_id=make_id(winner, o.ticker, o.side))
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
        attribution={o.ticker: {winner: 1.0} for o in orders if o.side == "buy"},
    )


def _from_targets(
    constructor: PortfolioConstructor,
    signals: dict[str, dict[str, float]],
    book: BookInput,
    market: MarketView,
    make_id: ClientIdFn,
) -> PipelineResult:
    if not signals:
        # Not one of the book's strategies was scored (failed to load, not
        # subscribed): hold rather than read it as "exit everything".
        _log.warning("pipeline.no_signals", held=sorted(_held(book.portfolio)))
        return PipelineResult(orders=[], target_book=TargetBook(), reason="no_signals")
    method = book.construction.signal_method or constructor.signal_method
    normalised = normalize(
        signals,
        method,
        long_only=constructor.settings.long_only,
        context=SignalContext(vols_annual=market.vols_annual),
    )
    target = constructor.target_weights(_construction_input(normalised, book, market))
    decision_date = market.as_of.date() if isinstance(market.as_of, datetime) else market.as_of
    raw = orders_from_targets(
        target.weights,
        book.portfolio,
        market.prices,
        buffer_fraction=book.construction.buffer_fraction,
        min_trade_weight=book.construction.min_trade_weight,
        as_of=decision_date,
    )
    owned = []
    for order in raw:
        owner = _dominant(target.attribution.get(order.ticker)) or _dominant(
            book.prior_attribution.get(order.ticker)
        )
        owned.append(
            replace(order, strategy_id=owner, client_id=make_id(owner, order.ticker, order.side))
        )
    kept, stale = _drop_stale(owned, market)
    risk = apply_book_risk(kept, book, market)
    return PipelineResult(
        orders=list(risk.orders),
        target_book=target,
        adjustments=list(risk.adjustments),
        stale_buys=stale,
        attribution={t: dict(s) for t, s in target.attribution.items() if t in target.weights},
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
    )


def _drop_stale(orders: Sequence[Order], market: MarketView) -> tuple[list[Order], list[str]]:
    if market.buyable is None:
        return list(orders), []
    return drop_stale_buys(orders, market.buyable)


def _held(portfolio: Portfolio) -> list[str]:
    return [t for t, q in portfolio.positions.items() if abs(q) > 1e-12]


def _dominant(shares: Mapping[str, float] | None) -> str | None:
    """The strategy with the largest share (ties: lowest id)."""
    if not shares:
        return None
    return min(shares, key=lambda sid: (-abs(shares[sid]), sid))


def _default_client_id(as_of: date | datetime) -> ClientIdFn:
    def make(strategy_id: str | None, ticker: str, side: OrderSide) -> str:
        return make_client_id(
            as_of=as_of,  # type: ignore[arg-type]
            strategy_id=strategy_id or PORTFOLIO_STRATEGY,
            ticker=ticker,
            side=side,
        )

    return make

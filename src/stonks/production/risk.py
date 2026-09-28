"""Risk layer between ``strategy.decide`` and the broker (roadmap 2.3, BL-11).

``apply_risk`` takes a strategy's proposed orders and returns the orders
that are allowed to reach the broker, plus one ``RiskAdjustment`` per order
it clipped or dropped. It runs every rule registered under
``stonks.production.rules`` in ``order``; today's caps live in
``rules/caps.py`` and only ever *reduce* exposure:

- sells are never blocked; they are only clipped to the held quantity so a
  sell cannot flip into a short, and they are placed before buys so their
  freed slots and proceeds are real by the time buys are placed;
- buys are clipped, in order, by ``max_open_positions``,
  ``max_weight_per_ticker``, ``max_weight_per_asset_class`` and the cash
  buffer (net of the expected fill costs: the cost model when given, else
  legacy slippage and fees), then dropped if their notional falls below
  ``min_order_notional``.

All weights are measured against the portfolio value before the tick's
orders. The function is pure: the portfolio passed in is not mutated.

Rules that need history (bars, equity curve, entry dates) only run when a
``RiskContext`` from ``build_risk_context`` is passed as ``context=``;
without it they are skipped and listed in ``RiskResult.skipped_rules``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Any

from stonks.accounts.models import DEFAULT_PORTFOLIO_ID
from stonks.backtest.costs import CostModel, CostModelSettings
from stonks.config import RiskPolicy
from stonks.core.types import Order, Portfolio
from stonks.logging import get_logger
from stonks.production import rules as _rules
from stonks.production.ledger import ledger_filter
from stonks.production.prices import load_history
from stonks.production.rules import OrderRule, RiskAdjustment, RiskContext, RiskRule
from stonks.production.rules.style_exposure import wants_exposures
from stonks.store.lake import DuckDBLake
from stonks.store.state import SqliteState

__all__ = [
    "RiskAdjustment",
    "RiskContext",
    "RiskPolicy",
    "RiskResult",
    "apply_risk",
    "build_risk_context",
    "entry_dates_from_fills",
    "model_book_risk_context",
    "needs_risk_context",
]

_log = get_logger("stonks.production.risk")

#: Bars of history per ticker a ``RiskContext`` carries (about a year).
HISTORY_BARS = 260


@dataclass(frozen=True)
class RiskResult:
    orders: list[Order]
    adjustments: list[RiskAdjustment]
    #: Rules that needed a context (history) and did not run.
    skipped_rules: list[str] = field(default_factory=list)


def apply_risk(
    orders: Sequence[Order],
    portfolio: Portfolio,
    prices: Mapping[str, float],
    asset_classes: Mapping[str, str],
    policy: RiskPolicy,
    *,
    slippage_bps: float = 0.0,
    fee_per_trade: float = 0.0,
    cost_model: CostModel | CostModelSettings | None = None,
    volumes: Mapping[str, float] | None = None,
    context: RiskContext | None = None,
    allow_short: bool = False,
) -> RiskResult:
    """``cost_model`` (exclusive with ``slippage_bps`` / ``fee_per_trade``)
    prices the cash estimate like the broker will; ``volumes`` feed its
    impact term. ``context`` adds history for rules that need it; the
    explicit arguments win over its fields. ``allow_short`` (the book's
    switch, or the context's) lets opening sells through the caps; the
    orders are then classified (split at zero) before any rule runs."""
    if not policy.enabled:
        return RiskResult(orders=list(orders), adjustments=[])
    model = cost_model.build() if isinstance(cost_model, CostModelSettings) else cost_model
    base = context or RiskContext(
        portfolio=portfolio, prices=prices, asset_classes=asset_classes, policy=policy
    )
    ctx = replace(
        base,
        portfolio=portfolio,
        prices=prices,
        asset_classes=asset_classes,
        policy=policy,
        cost_model=model if model is not None else base.cost_model,
        volumes=volumes if volumes is not None else base.volumes,
        slippage_bps=slippage_bps,
        fee_per_trade=fee_per_trade,
        allow_short=allow_short or base.allow_short,
    )

    rules: list[RiskRule] = []
    skipped: list[str] = []
    for rule in _rules.registered_rules():
        if not rule.enabled(policy):
            continue
        if rule.needs_history and context is None:
            skipped.append(rule.name)
        else:
            rules.append(rule)
    if skipped:
        _log.info("risk.rules_skipped", reason="no_context", rules=skipped)

    current = list(orders)
    if ctx.allow_short:
        from stonks.execution.orders import classify_all

        current = classify_all(current, portfolio.positions)
    adjustments: list[RiskAdjustment] = []
    for stage in _stages(rules):
        if isinstance(stage, list):
            current, adj = _rules.run_order_rules(stage, current, ctx)
        else:
            current, adj = stage.apply(current, ctx)
        adjustments.extend(adj)
    return RiskResult(orders=current, adjustments=adjustments, skipped_rules=skipped)


def needs_risk_context(*policies: RiskPolicy | None) -> bool:
    """Any of ``policies`` enables a rule that needs history, so the caller
    should build a ``RiskContext`` (otherwise the rule is skipped)."""
    return any(
        policy is not None
        and policy.enabled
        and any(rule.needs_history and rule.enabled(policy) for rule in _rules.registered_rules())
        for policy in policies
    )


def _stages(rules: Sequence[RiskRule]) -> list[list[OrderRule] | RiskRule]:
    """Group consecutive order rules into one pass; batch rules stand alone."""
    stages: list[list[OrderRule] | RiskRule] = []
    for rule in rules:
        if isinstance(rule, OrderRule):
            if stages and isinstance(stages[-1], list):
                stages[-1].append(rule)
            else:
                stages.append([rule])
        else:
            stages.append(rule)
    return stages


def build_risk_context(
    lake: DuckDBLake,
    state: SqliteState,
    portfolio: Portfolio,
    prices: Mapping[str, float],
    as_of: date,
    *,
    policy: RiskPolicy | None = None,
    universe: Sequence[str] = (),
    cost_model: CostModel | CostModelSettings | None = None,
    volumes: Mapping[str, float] | None = None,
    history_bars: int = HISTORY_BARS,
    portfolio_id: str = DEFAULT_PORTFOLIO_ID,
    overrides: Sequence[RiskPolicy | None] = (),
) -> RiskContext:
    """A full ``RiskContext`` for held, priced and ``universe`` tickers: the
    last ``history_bars`` adjusted bars (one query), asset classes and
    sectors, ``portfolio_id``'s equity curve and each holding's entry date
    (from that portfolio's fills only).

    ``overrides`` are the per-strategy policies the context also serves:
    style exposures are read when ``policy`` or any of them turns the style
    exposure rule on (22.10)."""
    from stonks.production.pnl import load_pnl

    held = [t for t, q in portfolio.positions.items() if abs(q) > 1e-12]
    tickers = sorted({*held, *prices, *universe})
    profiles = _profiles(lake, tickers)
    exposures = None
    if any(p is not None and wants_exposures(p) for p in (policy, *overrides)):
        from stonks.factors.style import safe_style_exposures

        exposures = safe_style_exposures(lake, tickers, as_of)
    return RiskContext(
        portfolio=portfolio,
        prices=dict(prices),
        asset_classes={t: c for t, (c, _) in profiles.items() if c},
        policy=policy or RiskPolicy(),
        history=load_history(lake, tickers, as_of, bars=history_bars),
        sectors={t: s for t, (_, s) in profiles.items() if s},
        equity_curve=[
            (r.day, r.total_value)
            for r in load_pnl(state, portfolio_id=portfolio_id)
            if r.day <= as_of
        ],
        entry_dates=_entry_dates(state, held, as_of, portfolio_id),
        portfolio_id=portfolio_id,
        cost_model=(
            cost_model.build() if isinstance(cost_model, CostModelSettings) else cost_model
        ),
        volumes=dict(volumes or {}),
        as_of=as_of,
        factor_exposures=exposures,
    )


def _profiles(lake: DuckDBLake, tickers: Sequence[str]) -> dict[str, tuple[str | None, str | None]]:
    if not tickers:
        return {}
    df = lake.sql(
        "SELECT id, asset_class, sector FROM instruments WHERE id = ANY(?)", [list(tickers)]
    )
    return {
        row.id: (_str_or_none(row.asset_class), _str_or_none(row.sector))
        for row in df.itertuples(index=False)
    }


def _str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _entry_dates(
    state: SqliteState, held: Sequence[str], as_of: date, portfolio_id: str
) -> dict[str, date]:
    """The day each held position was last opened from flat, from the fill
    history (fills of ``portfolio_id`` up to ``as_of``)."""
    if not held:
        return {}
    marks = ",".join("?" for _ in held)
    where, params = ledger_filter(state, "fills", portfolio_id, alias="f")
    rows = state.sql(
        f"""
        SELECT f.ticker, f.quantity, f.filled_at, o.side
          FROM fills f JOIN orders o ON o.client_id = f.order_client_id
         WHERE f.ticker IN ({marks}) AND substr(f.filled_at, 1, 10) <= ? AND {where}
         ORDER BY f.filled_at, f.id
        """,
        [*held, as_of.isoformat(), *params],
    )
    return entry_dates_from_fills(
        (r["ticker"], r["side"], float(r["quantity"]), date.fromisoformat(r["filled_at"][:10]))
        for r in rows
    )


def entry_dates_from_fills(
    fills: Iterable[tuple[str, str, float, date]],
) -> dict[str, date]:
    """``(ticker, side, quantity, day)`` fills, oldest first -> the day each
    still-open position, long or short, was last opened from flat or
    flipped to the other side (BE-04). Flat drops the date."""
    eps = 1e-12
    net: dict[str, float] = {}
    entry: dict[str, date] = {}
    for ticker, side, quantity, day in fills:
        before = net.get(ticker, 0.0)
        after = before + (quantity if side == "buy" else -quantity)
        net[ticker] = after
        if abs(after) <= eps:
            entry.pop(ticker, None)
        elif abs(before) <= eps or before * after < 0:
            entry[ticker] = day
    return entry


def model_book_risk_context(
    base: RiskContext,
    state: SqliteState,
    strategy_id: str,
    portfolio: Portfolio,
    as_of: date,
    *,
    book: Any = None,
) -> RiskContext:
    """``base`` (history and sectors, from :func:`build_risk_context`) for a
    model book: its virtual portfolio, its equity curve and the entry dates
    of its filled decisions. ``book`` (a ``production.shadow.BookStore``)
    names the book, by default the strategy's own model book."""
    from stonks.production.pnl import daily_pnl
    from stonks.production.shadow import shadow_book

    store = book if book is not None else shadow_book(strategy_id)
    held = [t for t, q in portfolio.positions.items() if abs(q) > 1e-12]
    entry: dict[str, date] = {}
    if held:
        entry = entry_dates_from_fills(store.filled(state, held, as_of))
    curve = [(r.day, r.total_value) for r in daily_pnl(store.curve(state)) if r.day <= as_of]
    return replace(
        base,
        portfolio=portfolio,
        equity_curve=curve,
        entry_dates=entry,
        portfolio_id=None,
    )

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

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date

from stonks.backtest.costs import CostModel, CostModelSettings
from stonks.config import RiskPolicy
from stonks.core.types import Order, Portfolio
from stonks.logging import get_logger
from stonks.production import rules as _rules
from stonks.production.prices import load_history
from stonks.production.rules import OrderRule, RiskAdjustment, RiskContext, RiskRule
from stonks.store.lake import DuckDBLake
from stonks.store.state import SqliteState

__all__ = [
    "RiskAdjustment",
    "RiskContext",
    "RiskPolicy",
    "RiskResult",
    "apply_risk",
    "build_risk_context",
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
) -> RiskResult:
    """``cost_model`` (exclusive with ``slippage_bps`` / ``fee_per_trade``)
    prices the cash estimate like the broker will; ``volumes`` feed its
    impact term. ``context`` adds history for rules that need it; the
    explicit arguments win over its fields."""
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
    )

    rules: list[RiskRule] = []
    skipped: list[str] = []
    for rule in _rules.registered_rules():
        if rule.needs_history and context is None:
            skipped.append(rule.name)
        else:
            rules.append(rule)
    if skipped:
        _log.info("risk.rules_skipped", reason="no_context", rules=skipped)

    current = list(orders)
    adjustments: list[RiskAdjustment] = []
    for stage in _stages(rules):
        if isinstance(stage, list):
            current, adj = _rules.run_order_rules(stage, current, ctx)
        else:
            current, adj = stage.apply(current, ctx)
        adjustments.extend(adj)
    return RiskResult(orders=current, adjustments=adjustments, skipped_rules=skipped)


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
) -> RiskContext:
    """A full ``RiskContext`` for held, priced and ``universe`` tickers: the
    last ``history_bars`` adjusted bars (one query), asset classes and
    sectors, the real portfolio's equity curve and each holding's entry date."""
    from stonks.production.pnl import load_pnl

    held = [t for t, q in portfolio.positions.items() if abs(q) > 1e-12]
    tickers = sorted({*held, *prices, *universe})
    profiles = _profiles(lake, tickers)
    return RiskContext(
        portfolio=portfolio,
        prices=dict(prices),
        asset_classes={t: c for t, (c, _) in profiles.items() if c},
        policy=policy or RiskPolicy(),
        history=load_history(lake, tickers, as_of, bars=history_bars),
        sectors={t: s for t, (_, s) in profiles.items() if s},
        equity_curve=[(r.day, r.total_value) for r in load_pnl(state) if r.day <= as_of],
        entry_dates=_entry_dates(state, held, as_of),
        cost_model=(
            cost_model.build() if isinstance(cost_model, CostModelSettings) else cost_model
        ),
        volumes=dict(volumes or {}),
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


def _entry_dates(state: SqliteState, held: Sequence[str], as_of: date) -> dict[str, date]:
    """The day each held position was last opened from flat, from the fill
    history (fills of the real portfolio up to ``as_of``)."""
    if not held:
        return {}
    marks = ",".join("?" for _ in held)
    rows = state.sql(
        f"""
        SELECT f.ticker, f.quantity, f.filled_at, o.side
          FROM fills f JOIN orders o ON o.client_id = f.order_client_id
         WHERE f.ticker IN ({marks}) AND substr(f.filled_at, 1, 10) <= ?
         ORDER BY f.filled_at, f.id
        """,
        [*held, as_of.isoformat()],
    )
    net: dict[str, float] = {}
    entry: dict[str, date] = {}
    for r in rows:
        before = net.get(r["ticker"], 0.0)
        after = before + (r["quantity"] if r["side"] == "buy" else -r["quantity"])
        net[r["ticker"]] = after
        if before <= 1e-12 < after:
            entry[r["ticker"]] = date.fromisoformat(r["filled_at"][:10])
        elif after <= 1e-12:
            entry.pop(r["ticker"], None)
    return entry

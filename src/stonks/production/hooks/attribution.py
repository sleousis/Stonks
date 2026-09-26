"""Position attribution (BL-12, W2.1): after each portfolio trades, record
which strategy (and subscription) each held position belongs to, as shares
summing to 1 per ticker, so P&L can be attributed per strategy per
portfolio (``position_attribution``, migration ``012``).

A ticker the pipeline decided on this tick takes its fresh shares (the
target book's split for a constructor, the deciding strategy for
``single_winner`` buys); every other holding carries its last recorded
shares. A re-run of the same day replaces that day's rows.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any

from stonks.production.hooks import PortfolioHookContext, PostTickHook, register_hook
from stonks.store.state import SqliteState

TABLE = "position_attribution"
_EPS = 1e-12


def attribution_enabled(state: SqliteState) -> bool:
    row = state.sql("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", [TABLE])
    return bool(row)


def load_attribution(
    state: SqliteState, portfolio_id: str, as_of: date
) -> dict[str, dict[str, float]]:
    """Per ticker, the shares of its latest attribution on or before
    ``as_of`` in ``portfolio_id`` (empty without the table)."""
    if not attribution_enabled(state):
        return {}
    rows = state.sql(
        f"""
        SELECT a.ticker, a.strategy_id, a.weight_share
          FROM {TABLE} a
          JOIN (SELECT ticker, MAX(as_of) AS as_of FROM {TABLE}
                 WHERE portfolio_id = ? AND as_of <= ? GROUP BY ticker) m
            ON m.ticker = a.ticker AND m.as_of = a.as_of
         WHERE a.portfolio_id = ?
         ORDER BY a.ticker, a.strategy_id
        """,
        [portfolio_id, as_of.isoformat(), portfolio_id],
    )
    out: dict[str, dict[str, float]] = {}
    for r in rows:
        out.setdefault(r["ticker"], {})[r["strategy_id"]] = float(r["weight_share"])
    return out


@register_hook
class PositionAttribution(PostTickHook):
    name = "position_attribution"
    stage = "portfolio"
    order = 10

    def run(self, ctx: PortfolioHookContext) -> Mapping[str, Any] | None:
        state = ctx.state
        if not ctx.scoped or not attribution_enabled(state):
            return None
        pipeline = ctx.pipeline
        fresh = pipeline.attribution if pipeline is not None else {}
        targets = pipeline.target_book.weights if pipeline is not None else {}
        decided = pipeline is not None and pipeline.decided_by is not None
        prior = load_attribution(state, ctx.portfolio_id, ctx.as_of)
        rows: list[list[Any]] = []
        now = _iso_now()
        for ticker, quantity in sorted(ctx.portfolio.positions.items()):
            if abs(quantity) <= _EPS:
                continue
            if ticker in fresh:
                shares, source = fresh[ticker], "decision" if decided else "target"
                target = None if decided else targets.get(ticker)
            elif ticker in prior:
                shares, source, target = prior[ticker], "carried", None
            else:
                continue
            for strategy_id, share in sorted(shares.items()):
                rows.append(
                    [
                        ctx.tick_id,
                        ctx.portfolio_id,
                        ctx.as_of.isoformat(),
                        ticker,
                        strategy_id,
                        ctx.subscription_ids.get(strategy_id),
                        float(quantity),
                        target,
                        float(share),
                        source,
                        now,
                    ]
                )
        state.execute(
            f"DELETE FROM {TABLE} WHERE portfolio_id = ? AND as_of = ?",
            [ctx.portfolio_id, ctx.as_of.isoformat()],
        )
        for row in rows:
            state.execute(
                f"""
                INSERT INTO {TABLE}
                    (tick_id, portfolio_id, as_of, ticker, strategy_id, subscription_id,
                     quantity, target_weight, weight_share, source, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                row,
            )
        return None


def _iso_now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat(timespec="seconds")

"""A live book's closed trades, for the protections (roadmap 19.6).

Each closing fill of the book's own ledger, per strategy and ticker, with
its realised P&L against the average cost of the position it closed.
Longs and shorts both count. ``stop`` is true when the closing order was a
stop (``decision_context.trigger == "stop"``, the protective stops of
19.10). Fees are left out: a protection asks "did it lose", not "by how
much after costs".
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from stonks.store.state import SqliteState

_EPS = 1e-9


@dataclass(frozen=True)
class ClosedTrade:
    strategy_id: str | None
    ticker: str
    exit_day: date
    pnl: float
    stop: bool = False
    #: The entry cost of the quantity this exit closed (the per-ticker loss
    #: breaker's denominator, roadmap 23.15).
    notional: float = 0.0

    @property
    def loss(self) -> bool:
        return self.pnl < 0


@dataclass(frozen=True)
class FillRow:
    strategy_id: str | None
    ticker: str
    side: str
    quantity: float
    price: float
    day: date
    stop: bool = False


def closed_from_fills(fills: Iterable[FillRow]) -> list[ClosedTrade]:
    """Replay fills (oldest first) per strategy and ticker at average cost.

    A fill whose own strategy holds nothing on the ticker but that reduces
    another strategy's position there (an exit a risk rule made with no
    strategy, or one the book's constructor gave to another owner) closes
    that position: the book sold what it held, and the trade belongs to
    the strategy that held it."""
    held: dict[tuple[str | None, str], float] = {}
    cost: dict[tuple[str | None, str], float] = {}
    out: list[ClosedTrade] = []

    def book(key: tuple[str | None, str], f: FillRow, signed: float) -> float:
        """Apply ``signed`` shares of ``f`` to ``key``; the shares left over
        once its position is closed (0 when it all went in)."""
        q = held.get(key, 0.0)
        if abs(q) <= _EPS or q * signed > 0:  # opens or grows
            cost[key] = cost.get(key, 0.0) + signed * f.price
            held[key] = q + signed
            return 0.0
        avg = cost.get(key, 0.0) / q
        closed = min(abs(signed), abs(q))
        direction = 1.0 if q > 0 else -1.0
        out.append(
            ClosedTrade(
                strategy_id=key[0],
                ticker=f.ticker,
                exit_day=f.day,
                pnl=direction * closed * (f.price - avg),
                stop=f.stop,
                notional=closed * abs(avg),
            )
        )
        rest = q + direction * -closed
        held[key] = rest
        cost[key] = avg * rest
        left = abs(signed) - closed
        return left if signed > 0 else -left

    for f in fills:
        key = (f.strategy_id, f.ticker)
        signed = f.quantity if f.side == "buy" else -f.quantity
        if abs(held.get(key, 0.0)) <= _EPS:
            for other, q in list(held.items()):
                if abs(signed) <= _EPS:
                    break
                if other[1] == f.ticker and other != key and q * signed < -_EPS:
                    signed = book(other, f, signed)
        if abs(signed) <= _EPS:
            continue
        left = book(key, f, signed)
        if abs(left) > _EPS:  # crossed zero: the rest opens the other side
            held[key] = left
            cost[key] = left * f.price
    return out


def closed_trades(
    state: SqliteState, portfolio_id: str, as_of: date, *, lookback_days: int = 120
) -> list[ClosedTrade]:
    """The book's trades closed in the ``lookback_days`` up to ``as_of``.
    Fills before the window still set the average cost. Manual orders are
    left out: they are the person's (``manual_discipline`` reads them)."""
    from stonks.production.ledger import ledger_columns  # the accounts import cycle

    cols = ledger_columns(state, "orders")
    ctx_col = "o.decision_context_json" if "decision_context_json" in cols else "NULL"
    # manual orders are the person's own, never the book's (BE-02)
    manual = " AND o.origin <> 'manual'" if "origin" in cols else ""
    rows = state.sql(
        f"SELECT o.strategy_id, f.ticker, o.side, f.quantity, f.price, f.filled_at, {ctx_col}"
        " AS ctx FROM fills f JOIN orders o ON o.client_id = f.order_client_id"
        f" WHERE f.portfolio_id = ? AND substr(f.filled_at, 1, 10) <= ?{manual}"
        " ORDER BY f.filled_at, f.id",
        [portfolio_id, as_of.isoformat()],
    )
    fills = [
        FillRow(
            strategy_id=r["strategy_id"],
            ticker=r["ticker"],
            side=r["side"],
            quantity=float(r["quantity"]),
            price=float(r["price"]),
            day=date.fromisoformat(str(r["filled_at"])[:10]),
            stop=_is_stop(r["ctx"]),
        )
        for r in rows
    ]
    start = as_of - timedelta(days=lookback_days)
    return [t for t in closed_from_fills(fills) if t.exit_day >= start]


def _is_stop(raw: object) -> bool:
    if not isinstance(raw, str) or not raw:
        return False
    try:
        ctx = json.loads(raw)
    except ValueError:
        return False
    return isinstance(ctx, dict) and ctx.get("trigger") == "stop"

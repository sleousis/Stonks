"""A book's manual trading today, for the manual discipline rule (roadmap
23.4).

Manual holdings are the person's own (``origin = 'manual'``), so their
fills replay on their own: per ticker a running position and average cost.
A fill that grows a position is an entry, one that shrinks it is an exit
with a realised P&L (fees in). Only rows stamped at or before ``now`` count
(P12). Entries still working at a broker (placed today, not filled yet)
count too, so a burst of orders cannot slip past the daily cap.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from stonks.production.rules._manual import ManualContext
from stonks.store.state import SqliteState

__all__ = ["ManualExit", "manual_context", "manual_exits"]

_EPS = 1e-9
_DONE_WITHOUT_FILL = ("rejected", "cancelled", "expired")


@dataclass(frozen=True)
class ManualExit:
    ticker: str
    filled_at: datetime
    quantity: float
    pnl: float


def _utc(raw: object) -> datetime:
    parsed = datetime.fromisoformat(str(raw))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _replay(state: SqliteState, portfolio_id: str, now: datetime):
    rows = state.sql(
        "SELECT f.order_client_id, f.ticker, f.quantity, f.price, f.fee, f.filled_at, o.side"
        " FROM fills f JOIN orders o ON o.client_id = f.order_client_id"
        " WHERE f.portfolio_id = ? AND o.origin = 'manual' ORDER BY f.filled_at, f.id",
        [portfolio_id],
    )
    held: dict[str, float] = {}
    cost: dict[str, float] = {}
    entries: dict[str, datetime] = {}
    exits: list[ManualExit] = []
    for r in rows:
        at = _utc(r["filled_at"])
        if at > now:
            continue
        ticker = str(r["ticker"])
        qty = float(r["quantity"])
        price = float(r["price"])
        fee = float(r["fee"] or 0.0)
        signed = qty if r["side"] == "buy" else -qty
        q = held.get(ticker, 0.0)
        if abs(q) <= _EPS or q * signed > 0:
            cost[ticker] = cost.get(ticker, 0.0) + signed * price + fee
            held[ticker] = q + signed
            entries.setdefault(str(r["order_client_id"]), at)
            continue
        closed = min(abs(signed), abs(q))
        avg = cost.get(ticker, 0.0) / q if abs(q) > _EPS else price
        direction = 1.0 if q > 0 else -1.0
        exit_fee = fee * closed / qty if qty else 0.0
        pnl = (price - avg) * closed * direction - exit_fee
        exits.append(ManualExit(ticker=ticker, filled_at=at, quantity=closed, pnl=pnl))
        after = q + signed
        if abs(after) <= _EPS:
            held.pop(ticker, None)
            cost.pop(ticker, None)
        elif after * q < 0:  # crossed zero: the rest opens the other way
            held[ticker] = after
            cost[ticker] = after * price
            entries.setdefault(str(r["order_client_id"]), at)
        else:
            held[ticker] = after
            cost[ticker] = avg * after
    return held, entries, exits


def manual_exits(state: SqliteState, portfolio_id: str, now: datetime) -> list[ManualExit]:
    """Every manual exit of the book up to ``now``, oldest first."""
    return _replay(state, portfolio_id, now)[2]


def manual_context(
    state: SqliteState,
    portfolio_id: str,
    now: datetime,
    *,
    live: bool,
    has_stop: bool,
) -> ManualContext:
    """What the manual discipline rule sees for one new manual order."""
    held, entries, exits = _replay(state, portfolio_id, now)
    today = now.date()
    filled_today = {cid for cid, at in entries.items() if at.date() == today}
    rows = state.sql(
        "SELECT ticker, side FROM orders WHERE portfolio_id = ? AND origin = 'manual'"
        " AND order_type IN ('market', 'limit') AND substr(created_at, 1, 10) = ?"
        f" AND status NOT IN ({','.join('?' for _ in _DONE_WITHOUT_FILL)})"
        " AND client_id NOT IN (SELECT order_client_id FROM fills)",
        [portfolio_id, today.isoformat(), *_DONE_WITHOUT_FILL],
    )
    # a working order that would open or grow the manual position is an
    # entry: a buy from flat or long, a short sale from flat or short
    working = [r for r in rows if _opens(held.get(r["ticker"], 0.0), r["side"])]
    losing = [e.filled_at for e in exits if e.pnl < -_EPS]
    return ManualContext(
        now=now,
        live=live,
        has_stop=has_stop,
        entries_today=len(filled_today) + len(working),
        pnl_today=sum(e.pnl for e in exits if e.filled_at.date() == today),
        last_losing_exit_at=max(losing) if losing else None,
    )


def _opens(held: float, side: str) -> bool:
    return held >= -_EPS if side == "buy" else held <= _EPS

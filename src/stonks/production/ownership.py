"""What an auto book owns in a connected broker account (BE-02).

A connected account can hold the user's own positions next to the ones the
book bought. The book trades only its own:

- **Owned** quantity per ticker is the book's net filled quantity in that
  portfolio (its fill ledger), carried through later splits.
- The book sees a **managed view** of the account: the account's cash plus,
  per ticker, the owned part of the holding (never more than the account
  holds, and nothing when the signs disagree). Strategies decide and the
  risk rules size against that view, so a strategy that exits "everything
  it did not pick" never sees the user's holdings.
- The rest is **external**: marked in the snapshot, never traded. An order
  that would take the book's position past zero into the user's holding
  (sell the user's long, cover the user's short) is dropped.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date

from stonks.core.corporate_actions import CorporateActions, Split
from stonks.core.types import Order, Portfolio
from stonks.store.state import SqliteState

_EPS = 1e-9


def owned_positions(
    state: SqliteState, portfolio_id: str, actions: CorporateActions | None = None
) -> dict[str, float]:
    """Net filled quantity per ticker of ``portfolio_id`` (signed: shorts
    are negative), each fill carried through the splits after its date."""
    rows = state.sql(
        "SELECT f.ticker, f.quantity, f.filled_at, o.side FROM fills f"
        " JOIN orders o ON o.client_id = f.order_client_id"
        " WHERE f.portfolio_id = ? ORDER BY f.filled_at",
        [portfolio_id],
    )
    out: dict[str, float] = {}
    for r in rows:
        ticker = r["ticker"]
        filled = date.fromisoformat(str(r["filled_at"])[:10])
        qty = float(r["quantity"]) * _split_factor(actions, ticker, filled)
        out[ticker] = out.get(ticker, 0.0) + (qty if r["side"] == "buy" else -qty)
    return {t: q for t, q in out.items() if abs(q) > _EPS}


def _split_factor(actions: CorporateActions | None, ticker: str, filled: date) -> float:
    factor = 1.0
    if actions is None:
        return factor
    for event in actions.for_ticker(ticker):
        if isinstance(event, Split) and event.ex_date > filled:
            factor *= event.ratio
    return factor


def managed_view(
    account: Portfolio, owned: Mapping[str, float]
) -> tuple[Portfolio, dict[str, float]]:
    """``(managed portfolio, external quantity per ticker)`` of ``account``."""
    managed: dict[str, float] = {}
    external: dict[str, float] = {}
    for ticker, qty in account.positions.items():
        if abs(qty) <= _EPS:
            continue
        mine = float(owned.get(ticker, 0.0))
        part = min(abs(mine), abs(qty)) if mine * qty > 0 else 0.0
        signed = part if qty > 0 else -part
        if part > _EPS:
            managed[ticker] = signed
        rest = qty - signed
        if abs(rest) > _EPS:
            external[ticker] = rest
    return Portfolio(cash=account.cash, positions=managed), external


def drop_unowned_crossings(
    orders: Sequence[Order], managed: Mapping[str, float], external: Mapping[str, float]
) -> tuple[list[Order], list[str]]:
    """Drop orders that would carry the book's position on a ticker the
    user also holds to the other side of zero: at the broker they would
    trade the user's own holding. Returns ``(kept, dropped tickers)``."""
    running = {t: float(q) for t, q in managed.items()}
    kept: list[Order] = []
    dropped: set[str] = set()
    for order in orders:
        theirs = external.get(order.ticker, 0.0)
        before = running.get(order.ticker, 0.0)
        after = before + (order.quantity if order.side == "buy" else -order.quantity)
        if abs(theirs) > _EPS and after * theirs < -_EPS:
            dropped.add(order.ticker)
            continue
        running[order.ticker] = after
        kept.append(order)
    return kept, sorted(dropped)

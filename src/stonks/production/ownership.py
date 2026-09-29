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
from stonks.production.ledger import ledger_columns
from stonks.store.state import SqliteState

_EPS = 1e-9


def owned_positions(
    state: SqliteState,
    portfolio_id: str,
    actions: CorporateActions | None = None,
    *,
    before: date | None = None,
) -> dict[str, float]:
    """Net filled quantity per ticker of ``portfolio_id`` (signed: shorts
    are negative), each fill carried through the splits after its date.
    Manual orders (roadmap 20.1) are the person's own, never the book's.
    ``before``: only fills dated before that day (what the book owned when
    the day began)."""
    return _net_fills(state, portfolio_id, actions, manual=False, before=before)


def manual_positions(
    state: SqliteState, portfolio_id: str, actions: CorporateActions | None = None
) -> dict[str, float]:
    """Net filled quantity per ticker of ``portfolio_id``'s manual orders
    (roadmap 20.1): holdings a person bought by hand, which the tick never
    trades."""
    if "origin" not in ledger_columns(state, "orders"):
        return {}
    return _net_fills(state, portfolio_id, actions, manual=True)


def _net_fills(
    state: SqliteState,
    portfolio_id: str,
    actions: CorporateActions | None,
    *,
    manual: bool,
    before: date | None = None,
) -> dict[str, float]:
    origin = ""
    if "origin" in ledger_columns(state, "orders"):
        origin = " AND o.origin = 'manual'" if manual else " AND o.origin <> 'manual'"
    rows = state.sql(
        "SELECT f.ticker, f.quantity, f.filled_at, o.side FROM fills f"
        " JOIN orders o ON o.client_id = f.order_client_id"
        f" WHERE f.portfolio_id = ?{origin} ORDER BY f.filled_at",
        [portfolio_id],
    )
    out: dict[str, float] = {}
    for r in rows:
        ticker = r["ticker"]
        filled = date.fromisoformat(str(r["filled_at"])[:10])
        if before is not None and filled >= before:
            continue
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


def strip_holdings(
    account: Portfolio, holdings: Mapping[str, float]
) -> tuple[Portfolio, dict[str, float]]:
    """``(account without holdings, the part taken out)``: the book's view
    of a simulated account that also holds manual positions (roadmap 20.1).
    Per ticker the part taken out is at most what the account holds, and
    nothing when the signs disagree."""
    managed: dict[str, float] = {}
    taken: dict[str, float] = {}
    for ticker, qty in account.positions.items():
        if abs(qty) <= _EPS:
            continue
        theirs = float(holdings.get(ticker, 0.0))
        part = min(abs(theirs), abs(qty)) if theirs * qty > 0 else 0.0
        signed = part if qty > 0 else -part
        if part > _EPS:
            taken[ticker] = signed
        rest = qty - signed
        if abs(rest) > _EPS:
            managed[ticker] = rest
    return Portfolio(cash=account.cash, positions=managed), taken


def merge_holdings(managed: Portfolio, holdings: Mapping[str, float]) -> Portfolio:
    """``managed`` with ``holdings`` added back (the whole account again)."""
    positions = dict(managed.positions)
    for ticker, qty in holdings.items():
        total = positions.get(ticker, 0.0) + qty
        if abs(total) > _EPS:
            positions[ticker] = total
        else:
            positions.pop(ticker, None)
    return Portfolio(cash=managed.cash, positions=positions)


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

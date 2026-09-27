"""Load the account rules' inputs from the state database (roadmap 19.7).

:func:`load_account_inputs` reads what the state holds for one live
portfolio: its profile, restricted list, key information document flags,
settlement entries, day trades and loss sales. What lives elsewhere comes
in from the caller: the broker's account state, instrument facts from the
lake, borrow availability.

Only the book's own fills count (its ledger), never the owner's own
trades in a shared account.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date, timedelta

import numpy as np

from stonks.accounts.rules import AccountRuleInputs, InstrumentFacts
from stonks.accounts.rules.profiles import get_profile
from stonks.accounts.rules.settlement import load_settlements
from stonks.execution.brokers.base import LiveAccountState
from stonks.production.rules._account_settings import AccountRulesSettings
from stonks.store.state import SqliteState

_EPS = 1e-9


def _has(state: SqliteState, table: str) -> bool:
    rows = state.sql("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", [table])
    return bool(rows)


def restricted_tickers(state: SqliteState, portfolio_id: str) -> dict[str, str]:
    if not _has(state, "account_restricted"):
        return {}
    rows = state.sql(
        "SELECT ticker, reason FROM account_restricted WHERE portfolio_id = ?", [portfolio_id]
    )
    return {r["ticker"]: r["reason"] for r in rows}


def kid_flags(state: SqliteState, jurisdiction: str) -> dict[str, bool]:
    if jurisdiction == "us" or not _has(state, "product_documents"):
        return {}
    rows = state.sql(
        "SELECT ticker, kid_available FROM product_documents WHERE jurisdiction = ?",
        [jurisdiction],
    )
    return {r["ticker"]: bool(r["kid_available"]) for r in rows}


def _fills(
    state: SqliteState, portfolio_id: str, until: date
) -> list[tuple[str, str, float, float, date]]:
    rows = state.sql(
        "SELECT f.ticker, o.side, f.quantity, f.price, f.filled_at FROM fills f"
        " JOIN orders o ON o.client_id = f.order_client_id"
        " WHERE f.portfolio_id = ? AND substr(f.filled_at, 1, 10) <= ?"
        " ORDER BY f.filled_at, f.id",
        [portfolio_id, until.isoformat()],
    )
    return [
        (
            r["ticker"],
            r["side"],
            float(r["quantity"]),
            float(r["price"]),
            date.fromisoformat(str(r["filled_at"])[:10]),
        )
        for r in rows
    ]


def window_start(as_of: date, business_days: int) -> date:
    """The first day of a window of ``business_days`` ending on ``as_of``."""
    start = np.busday_offset(np.datetime64(as_of), -(business_days - 1), roll="backward")
    return date.fromisoformat(str(start))


def day_trades(
    fills: list[tuple[str, str, float, float, date]], as_of: date, window_days: int
) -> list[date]:
    """One date per day trade in the window: a close (a sell of a long, a
    buy to cover a short) of a position opened the same day. Opening again
    after a counted close starts a new one, so two round trips in a day
    are two day trades, while selling an older holding and buying back is
    none. Longs and shorts both count."""
    start = window_start(as_of, window_days)
    out: list[date] = []
    held: dict[str, float] = {}
    #: Per ticker: the day of an opening not yet matched by a close.
    open_day: dict[str, date] = {}
    for ticker, side, qty, _, day in fills:
        q = held.get(ticker, 0.0)
        signed = qty if side == "buy" else -qty
        closes = (q > _EPS and signed < 0) or (q < -_EPS and signed > 0)
        opens = abs(q + signed) > abs(q) + _EPS or (closes and abs(signed) > abs(q) + _EPS)
        if closes and open_day.get(ticker) == day:
            if start <= day <= as_of:
                out.append(day)
            open_day.pop(ticker, None)
        if opens:
            open_day[ticker] = day
        held[ticker] = q + signed
    return sorted(out)


def opened_on(fills: list[tuple[str, str, float, float, date]], as_of: date) -> frozenset[str]:
    """Tickers with a position opened or grown on ``as_of`` (a buy of a long
    or a sell to open a short)."""
    held: dict[str, float] = {}
    out: set[str] = set()
    for ticker, side, qty, _, day in fills:
        q = held.get(ticker, 0.0)
        new = q + (qty if side == "buy" else -qty)
        if day == as_of and (abs(new) > abs(q) + _EPS or q * new < 0):
            out.add(ticker)
        held[ticker] = new
    return frozenset(out)


def loss_sales(
    fills: list[tuple[str, str, float, float, date]], as_of: date, window_days: int
) -> dict[str, date]:
    """The last sale at a loss per ticker within ``window_days`` of
    ``as_of``, against the average cost of the long it sold from."""
    held: dict[str, float] = {}
    cost: dict[str, float] = {}
    out: dict[str, date] = {}
    earliest = as_of - timedelta(days=window_days)
    for ticker, side, qty, price, day in fills:
        q = held.get(ticker, 0.0)
        if side == "buy":
            cost[ticker] = cost.get(ticker, 0.0) + qty * price
            held[ticker] = q + qty
            continue
        if q <= _EPS:
            continue
        avg = cost.get(ticker, 0.0) / q
        sold = min(qty, q)
        if price < avg - _EPS and day >= earliest:
            out[ticker] = day
        held[ticker] = q - sold
        cost[ticker] = avg * (q - sold)
    return out


def load_account_inputs(
    state: SqliteState,
    portfolio_id: str,
    as_of: date,
    settings: AccountRulesSettings,
    *,
    account: LiveAccountState | None = None,
    instruments: Mapping[str, InstrumentFacts] | None = None,
    shortable: Mapping[str, float | None] | None = None,
    short_sale_restricted: frozenset[str] = frozenset(),
    currency_of: Callable[[str], str] | None = None,
) -> AccountRuleInputs | None:
    """The inputs for ``portfolio_id`` on the execution session ``as_of``,
    or ``None`` when the portfolio has no account profile (then the
    ``account_rules`` risk rule refuses every opening order)."""
    profile = get_profile(state, portfolio_id)
    if profile is None:
        return None
    facts = dict(instruments or {})

    def _ccy(ticker: str) -> str:
        if currency_of is not None:
            return currency_of(ticker)
        f = facts.get(ticker)
        return (f.currency or profile.base_currency).upper() if f else profile.base_currency

    fills = _fills(state, portfolio_id, as_of)
    return AccountRuleInputs(
        profile=profile,
        as_of=as_of,
        account=account,
        instruments=facts,
        restricted=restricted_tickers(state, portfolio_id),
        kid_available=kid_flags(state, profile.jurisdiction),
        settlements=load_settlements(state, portfolio_id, settings, currency_of=_ccy, as_of=as_of),
        day_trades=day_trades(fills, as_of, settings.pdt_window_days),
        opened_today=opened_on(fills, as_of),
        loss_sales=loss_sales(fills, as_of, settings.wash_sale_window_days),
        shortable=dict(shortable or {}),
        short_sale_restricted=short_sale_restricted,
    )

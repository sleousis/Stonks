"""Settlement per market (roadmap 19.7).

Each fill's cash settles a number of business days after the trade, set by
the security's market, not the account's jurisdiction
(``AccountRulesSettings.settlement_days``: US T+1, EU and UK T+2 today).
Weekends are skipped. Exchange holidays are not, so our date can be a day
early: the broker's settled cash is the source of truth and the stricter
figure wins.

:func:`record_settlements` writes the ``settlement_ledger`` rows of fills
that have none yet (idempotent, one row per fill). :func:`load_settlements`
reads them, and computes the rows of fills not recorded yet, so a read
never needs a write first.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, timedelta
from typing import Any

import numpy as np

from stonks.accounts.rules import SettlementEntry, market_of
from stonks.production.rules._account_settings import AccountRulesSettings
from stonks.store.state import SqliteState

TABLE = "settlement_ledger"
#: How far back unsettled cash can reach (the longest cycle plus weekends).
LOOKBACK_DAYS = 14

CurrencyOf = Callable[[str], str]


def settle_date(trade_date: date, market: str, settings: AccountRulesSettings) -> date:
    """``trade_date`` plus the market's cycle in business days."""
    days = settings.cycle(market)
    if days == 0:
        return trade_date
    rolled = np.busday_offset(np.datetime64(trade_date), days, roll="forward")
    return date.fromisoformat(str(rolled))


def settlements_enabled(state: SqliteState) -> bool:
    rows = state.sql("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", [TABLE])
    return bool(rows)


def _unrecorded_fills(state: SqliteState, portfolio_id: str, since: date) -> list[Any]:
    joined = settlements_enabled(state)
    missing = " AND NOT EXISTS (SELECT 1 FROM settlement_ledger s WHERE s.fill_id = f.id)"
    rows = state.sql(
        "SELECT f.id, f.ticker, f.quantity, f.price, f.fee, f.filled_at, o.side"
        " FROM fills f JOIN orders o ON o.client_id = f.order_client_id"
        " WHERE f.portfolio_id = ? AND substr(f.filled_at, 1, 10) >= ?"
        + (missing if joined else "")
        + " ORDER BY f.id",
        [portfolio_id, since.isoformat()],
    )
    return list(rows)


def _entry(row: Any, settings: AccountRulesSettings, currency_of: CurrencyOf) -> SettlementEntry:
    ticker = str(row["ticker"])
    side = "buy" if row["side"] == "buy" else "sell"
    qty = float(row["quantity"])
    price = float(row["price"])
    fee = float(row["fee"] or 0.0)
    trade = date.fromisoformat(str(row["filled_at"])[:10])
    amount = -(qty * price + fee) if side == "buy" else qty * price - fee
    return SettlementEntry(
        ticker=ticker,
        side=side,
        currency=currency_of(ticker),
        amount=amount,
        trade_date=trade,
        settle_date=settle_date(trade, market_of(ticker), settings),
    )


def record_settlements(
    state: SqliteState,
    portfolio_id: str,
    settings: AccountRulesSettings,
    *,
    currency_of: CurrencyOf,
    as_of: date,
) -> int:
    """Write the ledger rows of this portfolio's recent fills that have none.
    Returns how many were written."""
    if not settlements_enabled(state):
        return 0
    rows = _unrecorded_fills(state, portfolio_id, as_of - timedelta(days=LOOKBACK_DAYS))
    with state.transaction():
        for row in rows:
            e = _entry(row, settings, currency_of)
            state.execute(
                f"INSERT OR IGNORE INTO {TABLE} (portfolio_id, fill_id, ticker, side, currency,"
                " amount, trade_date, settle_date) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    portfolio_id,
                    row["id"],
                    e.ticker,
                    e.side,
                    e.currency,
                    e.amount,
                    e.trade_date.isoformat(),
                    e.settle_date.isoformat(),
                ],
            )
    return len(rows)


def load_settlements(
    state: SqliteState,
    portfolio_id: str,
    settings: AccountRulesSettings,
    *,
    currency_of: CurrencyOf,
    as_of: date,
) -> list[SettlementEntry]:
    """The portfolio's recent settlement entries: the recorded rows plus
    the computed rows of fills not recorded yet."""
    since = as_of - timedelta(days=LOOKBACK_DAYS)
    out: list[SettlementEntry] = []
    if settlements_enabled(state):
        for r in state.sql(
            f"SELECT ticker, side, currency, amount, trade_date, settle_date FROM {TABLE}"
            " WHERE portfolio_id = ? AND trade_date >= ? ORDER BY id",
            [portfolio_id, since.isoformat()],
        ):
            out.append(
                SettlementEntry(
                    ticker=r["ticker"],
                    side=r["side"],
                    currency=r["currency"],
                    amount=float(r["amount"]),
                    trade_date=date.fromisoformat(r["trade_date"]),
                    settle_date=date.fromisoformat(r["settle_date"]),
                )
            )
    out += [
        _entry(row, settings, currency_of) for row in _unrecorded_fills(state, portfolio_id, since)
    ]
    return out

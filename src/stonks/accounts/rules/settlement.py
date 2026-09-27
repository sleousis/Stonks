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

A row holds one currency: the security's. A broker can report the
commission in another one (``fills.fee_currency``), so the fee is
converted with :class:`stonks.fx.FxRates` first. With no rate the fee is
left out of the computed row, which counts sale proceeds in full (the
stricter settled cash), and the row is not recorded until a rate exists.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, timedelta
from typing import Any

import numpy as np

from stonks.accounts.rules import SettlementEntry, market_of
from stonks.fx import FxRates, normalize_currency
from stonks.logging import get_logger
from stonks.production.rules._account_settings import AccountRulesSettings
from stonks.store.state import SqliteState

TABLE = "settlement_ledger"
#: How far back unsettled cash can reach (the longest cycle plus weekends).
LOOKBACK_DAYS = 14

CurrencyOf = Callable[[str], str]

_log = get_logger(__name__)


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
        "SELECT f.id, f.ticker, f.quantity, f.price, f.fee, f.fee_currency, f.filled_at, o.side"
        " FROM fills f JOIN orders o ON o.client_id = f.order_client_id"
        " WHERE f.portfolio_id = ? AND substr(f.filled_at, 1, 10) >= ?"
        + (missing if joined else "")
        + " ORDER BY f.id",
        [portfolio_id, since.isoformat()],
    )
    return list(rows)


def fee_currencies(state: SqliteState, portfolio_id: str, as_of: date) -> set[str]:
    """The commission currencies of the portfolio's recent fills (to load
    only the FX rates a settlement read needs)."""
    since = (as_of - timedelta(days=LOOKBACK_DAYS)).isoformat()
    rows = state.sql(
        "SELECT DISTINCT fee_currency FROM fills WHERE portfolio_id = ?"
        " AND fee_currency IS NOT NULL AND substr(filled_at, 1, 10) >= ?",
        [portfolio_id, since],
    )
    return {str(r["fee_currency"]) for r in rows}


def _fee(row: Any, currency: str, trade: date, fx: FxRates | None) -> float | None:
    """The fill's fee in ``currency``, or ``None`` when its own currency has
    no rate."""
    fee = float(row["fee"] or 0.0)
    source = row["fee_currency"]
    if not fee or not source or normalize_currency(source) == normalize_currency(currency):
        return fee
    converted = None if fx is None else fx.convert(fee, str(source), currency, trade)
    if converted is None:
        _log.warning("settlement.fee_fx_missing", fill_id=row["id"], source=source, target=currency)
    return converted


def _entry(
    row: Any, settings: AccountRulesSettings, currency_of: CurrencyOf, fx: FxRates | None
) -> tuple[SettlementEntry, bool]:
    """The fill's entry, and whether its fee was known in the row currency."""
    ticker = str(row["ticker"])
    side = "buy" if row["side"] == "buy" else "sell"
    qty = float(row["quantity"])
    price = float(row["price"])
    currency = currency_of(ticker)
    trade = date.fromisoformat(str(row["filled_at"])[:10])
    fee = _fee(row, currency, trade, fx)
    known = fee is not None
    fee = fee or 0.0
    amount = -(qty * price + fee) if side == "buy" else qty * price - fee
    entry = SettlementEntry(
        ticker=ticker,
        side=side,
        currency=currency,
        amount=amount,
        trade_date=trade,
        settle_date=settle_date(trade, market_of(ticker), settings),
    )
    return entry, known


def record_settlements(
    state: SqliteState,
    portfolio_id: str,
    settings: AccountRulesSettings,
    *,
    currency_of: CurrencyOf,
    as_of: date,
    fx: FxRates | None = None,
) -> int:
    """Write the ledger rows of this portfolio's recent fills that have none.
    Returns how many were written. A fill whose fee has no FX rate waits."""
    if not settlements_enabled(state):
        return 0
    rows = _unrecorded_fills(state, portfolio_id, as_of - timedelta(days=LOOKBACK_DAYS))
    written = 0
    with state.transaction():
        for row in rows:
            e, known = _entry(row, settings, currency_of, fx)
            if not known:
                continue
            written += 1
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
    return written


def load_settlements(
    state: SqliteState,
    portfolio_id: str,
    settings: AccountRulesSettings,
    *,
    currency_of: CurrencyOf,
    as_of: date,
    fx: FxRates | None = None,
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
        _entry(row, settings, currency_of, fx)[0]
        for row in _unrecorded_fills(state, portfolio_id, since)
    ]
    return out

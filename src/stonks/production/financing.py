"""Durable financing clock of the tick (roadmap 16.1).

The tick builds a short book's paper broker fresh from the snapshot, so
the broker cannot know when financing was last charged. The tick asks
:func:`last_accrual` and calls ``broker.accrue(as_of, since=...)``, then
writes the charges and the new date with :func:`record_accrual` inside the
transaction that writes its snapshot. A book with no stored date starts
from its latest tick snapshot (the tick passes that date), so the first
tick after an upgrade still charges the days since the book last traded."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, time
from typing import TYPE_CHECKING, Any

from stonks.store.state import SqliteState

if TYPE_CHECKING:
    from stonks.backtest.simulated_broker import FinancingEvent
    from stonks.config import RiskPolicy
    from stonks.execution.borrow import BorrowSource
    from stonks.execution.margin import MarginModel


def short_account(policy: RiskPolicy) -> tuple[MarginModel, BorrowSource | None]:
    """The margin model and borrow source a short book's paper account
    trades with (BE-30): the ``margin_call`` rule's model (Reg T when that
    model cannot short) and a ``FlatBorrow`` over the ``borrow`` settings of
    ``borrow_check``, else of ``squeeze_guard``. ``None`` borrow: none is
    configured, the broker keeps its default source."""
    from stonks.execution.margin import RegTMargin

    rules = policy.rules
    model = rules.margin_call.margin.build()
    margin = model if model.allows_short else RegTMargin()
    settings = rules.borrow_check.borrow or rules.squeeze_guard.borrow
    return margin, (settings.build() if settings is not None else None)


def broker_borrow_source(broker: Any, lake: Any) -> BorrowSource | None:
    """The borrow source a short book at a real broker trades with (roadmap
    19.14): the broker's own locate (``BorrowLocator``, IBKR's shortable
    ticks), with the lake's ``borrow_rates`` for the fee. ``None`` when the
    broker has no locate: the book keeps the settings' source."""
    from stonks.execution.borrow import LakeBorrowSource
    from stonks.execution.brokers.base import BorrowLocator

    if broker is None or not isinstance(broker, BorrowLocator):
        return None
    fees = LakeBorrowSource(lake) if callable(getattr(lake, "borrow_rate", None)) else None
    return broker.borrow_source(fees)


def live_short_financing(
    positions: Mapping[str, float],
    prices: Mapping[str, float],
    as_of: date,
    *,
    since: date | None,
    borrow: BorrowSource,
    asset_classes: Mapping[str, str] | None = None,
) -> list[FinancingEvent]:
    """The borrow fees a short book at a real broker pays (roadmap 19.13):
    each own short, at the broker's borrow rate for ``as_of`` (IBKR's, with
    the lake's ``borrow_rates`` from its short stock files), for the
    calendar days since ``since``. The broker debits the real fee. These
    rows put it in the book's P&L the day it is owed. Nothing without a
    start date, a new day, a price or a quote."""
    from stonks.backtest.simulated_broker import FinancingEvent
    from stonks.execution.borrow import daily_fee

    if since is None or as_of <= since:
        return []
    days = (as_of - since).days
    stamp = datetime.combine(as_of, time(), UTC)
    classes = asset_classes or {}
    events: list[FinancingEvent] = []
    for ticker, qty in sorted(positions.items()):
        price = prices.get(ticker)
        if qty >= 0 or not price or price <= 0:
            continue
        quote = borrow.quote(ticker, as_of, classes.get(ticker, "equity"))  # type: ignore[arg-type]
        if quote is None or not quote.shortable:
            continue
        fee = daily_fee(qty, price, quote, days)
        if fee > 0:
            events.append(FinancingEvent(stamp, ticker, "borrow_fee", -fee, days))
    return events


def last_accrual(state: SqliteState, portfolio_id: str) -> date | None:
    """The stored accrual date of ``portfolio_id``, or ``None``."""
    rows = state.sql(
        "SELECT accrued_through FROM financing_accruals WHERE portfolio_id = ?", [portfolio_id]
    )
    return date.fromisoformat(rows[0]["accrued_through"]) if rows else None


def record_accrual(
    state: SqliteState,
    portfolio_id: str,
    as_of: date,
    events: Sequence[Any],
    *,
    tick_id: str | None = None,
) -> None:
    """Write ``events`` (``FinancingEvent``) and move the clock to
    ``as_of`` (never backwards). Call inside the snapshot's transaction."""
    now = datetime.now(UTC).isoformat(timespec="seconds")
    for event in events:
        state.execute(
            "INSERT INTO financing_charges (portfolio_id, tick_id, as_of, ticker, kind, amount,"
            " days, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                portfolio_id,
                tick_id,
                as_of.isoformat(),
                event.ticker,
                event.kind,
                float(event.amount),
                int(event.days),
                now,
            ],
        )
    state.execute(
        "INSERT INTO financing_accruals (portfolio_id, accrued_through, updated_at)"
        " VALUES (?, ?, ?) ON CONFLICT (portfolio_id) DO UPDATE SET"
        " accrued_through = MAX(accrued_through, excluded.accrued_through),"
        " updated_at = excluded.updated_at",
        [portfolio_id, as_of.isoformat(), now],
    )

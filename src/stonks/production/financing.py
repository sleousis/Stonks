"""Durable financing clock of the tick (roadmap 16.1).

The tick builds a short book's paper broker fresh from the snapshot, so
the broker cannot know when financing was last charged. The tick asks
:func:`last_accrual` and calls ``broker.accrue(as_of, since=...)``, then
writes the charges and the new date with :func:`record_accrual` inside the
transaction that writes its snapshot. A book with no stored date starts
from its latest tick snapshot (the tick passes that date), so the first
tick after an upgrade still charges the days since the book last traded."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any

from stonks.store.state import SqliteState

if TYPE_CHECKING:
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

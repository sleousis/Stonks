"""Test helpers for the auto gate: paper days (``portfolio_runs`` rows) and
a broker portfolio linked to a trading connection."""

from __future__ import annotations

from datetime import date, timedelta

from stonks.accounts.audit import iso_now
from stonks.production.portfolio_runs import PortfolioRun, record_run
from stonks.store.state import SqliteState

NOW = "2026-01-01T00:00:00+00:00"


def seed_paper_days(
    state: SqliteState,
    subscription_id: str,
    n: int,
    *,
    start: date = date(2026, 1, 1),
    breached: bool = False,
    portfolio_id: str = "pf_default",
) -> None:
    """``n`` completed paper days of ``subscription_id``, one per calendar
    day from ``start``, by ticks that started now."""
    now = iso_now()
    for i in range(n):
        day = start + timedelta(days=i)
        record_run(
            state,
            PortfolioRun(
                tick_id=f"tick_{subscription_id}_{day.isoformat()}",
                portfolio_id=portfolio_id,
                as_of=day,
                mode="paper",
                status="ok",
                risk_breached=breached,
                paper_subscriptions=(subscription_id,),
                started_at=now,
                finished_at=now,
            ),
        )


def link_connection(
    state: SqliteState,
    portfolio_id: str,
    *,
    provider: str = "fake_trading",
    status: str = "active",
    external_account_id: str = "fake-acc-1",
    token: str | None = None,
) -> str:
    """Link ``portfolio_id`` to a new connection row of its owner (no
    credentials; the tick tests pass their own trader factory)."""
    owner = state.sql("SELECT owner_id FROM portfolios WHERE id = ?", [portfolio_id])[0][0]
    connection_id = f"con_{portfolio_id}"
    state.execute(
        "INSERT INTO broker_connections (id, user_id, provider, label, status, created_at,"
        " updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [connection_id, owner, provider, token, status, NOW, NOW],
    )
    state.execute(
        "UPDATE portfolios SET broker_connection_id = ?, external_account_id = ? WHERE id = ?",
        [connection_id, external_account_id, portfolio_id],
    )
    return connection_id

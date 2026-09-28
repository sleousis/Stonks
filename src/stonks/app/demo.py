"""DemoService: the sample portfolio a new person can open (roadmap 23.17).

The demo is built from synthetic bars on each read (:mod:`stonks.accounts.demo`).
It never touches real books, orders, fills or the lake: the only row is
the person's seed in ``demo_portfolios``. Every view carries
``label = "Sample data"`` so the console can mark it plainly.

- Reading, opening and removing your demo need ``data.read``: a viewer can
  take the tour too. Each person sees only their own demo.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from datetime import UTC, date, datetime

from pydantic import BaseModel

from stonks.accounts.audit import iso_now
from stonks.accounts.demo import DEMO_LABEL, build_demo
from stonks.app.context import AppContext
from stonks.app.errors import ValidationError
from stonks.auth.policy import Permission, require
from stonks.auth.principal import Principal


class DemoPositionView(BaseModel):
    ticker: str
    name: str
    sector: str
    quantity: float
    cost: float
    price: float
    value: float
    weight: float
    pnl: float
    pnl_pct: float


class DemoPointView(BaseModel):
    day: date
    value: float


class DemoPortfolioView(BaseModel):
    """Your demo portfolio. ``exists`` is false until you open one: then
    the other fields are empty."""

    exists: bool
    #: Always "Sample data": the figures are made up.
    label: str = DEMO_LABEL
    name: str | None = None
    as_of: date | None = None
    currency: str = "USD"
    start_value: float | None = None
    cash: float | None = None
    total_value: float | None = None
    total_return: float | None = None
    day_change: float | None = None
    positions: list[DemoPositionView] = []
    curve: list[DemoPointView] = []
    created_at: datetime | None = None


def _person(principal: Principal) -> str:
    if principal.scope.is_service:
        raise ValidationError("the demo portfolio is for people, not services")
    return principal.user_id


class DemoService:
    def __init__(self, context: AppContext, *, clock: Callable[[], datetime] | None = None) -> None:
        self._ctx = context
        self._clock = clock or (lambda: datetime.now(UTC))

    def get(self, principal: Principal) -> DemoPortfolioView:
        require(principal, Permission.READ)
        user_id = _person(principal)
        with self._ctx.state() as state:
            rows = state.sql(
                "SELECT seed, created_at FROM demo_portfolios WHERE user_id = ?", [user_id]
            )
        if not rows:
            return DemoPortfolioView(exists=False)
        return self._view(int(rows[0]["seed"]), rows[0]["created_at"])

    def open(self, principal: Principal) -> DemoPortfolioView:
        """Open your demo (the same one again when it exists)."""
        require(principal, Permission.READ)
        user_id = _person(principal)
        with self._ctx.state() as state, state.transaction():
            state.execute(
                "INSERT OR IGNORE INTO demo_portfolios (user_id, seed, created_at)"
                " VALUES (?, ?, ?)",
                [user_id, secrets.randbelow(1_000_000) + 1, iso_now()],
            )
        return self.get(principal)

    def remove(self, principal: Principal) -> None:
        """Remove your demo. Nothing else changes: it never held real data."""
        require(principal, Permission.READ)
        user_id = _person(principal)
        with self._ctx.state() as state, state.transaction():
            state.execute("DELETE FROM demo_portfolios WHERE user_id = ?", [user_id])

    def _view(self, seed: int, created_at: str) -> DemoPortfolioView:
        book = build_demo(seed=seed, as_of=self._clock().date())
        return DemoPortfolioView(
            exists=True,
            label=book.label,
            name=book.name,
            as_of=book.as_of,
            currency=book.currency,
            start_value=book.start_value,
            cash=book.cash,
            total_value=book.total_value,
            total_return=book.total_return,
            day_change=book.day_change,
            positions=[DemoPositionView(**p.__dict__) for p in book.positions],
            curve=[DemoPointView(day=p.day, value=round(p.value, 2)) for p in book.curve],
            created_at=datetime.fromisoformat(created_at),
        )

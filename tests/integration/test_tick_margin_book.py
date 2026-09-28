"""A live margin book in the tick (roadmap 19.13): each run records the
broker's margin cushion and alerts when it is thin, and a dry run records
nothing."""

from __future__ import annotations

import pytest

import tests.integration.test_tick_modes as modes
from stonks.execution.brokers.base import LiveAccountState
from stonks.production.live.margin import latest_check, recent_checks
from tests.integration.test_tick_tickets import DAY1, _tick


@pytest.fixture(autouse=True)
def _clean_fakes():
    modes.fake.FAKE_BOOKS.clear()
    modes.reset_limiters()
    yield
    modes.fake.FAKE_BOOKS.clear()
    modes.reset_limiters()


@pytest.fixture
def world(tmp_path, lake_trending):
    w = modes.World(tmp_path, lake_trending)
    yield w
    w.state.close()


class _MarginTrader:
    """A trader whose account is a margin account with a thin cushion."""

    def __init__(self, inner, excess: float) -> None:
        self._inner = inner
        self.excess = excess

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def fetch_portfolio(self):
        return self._inner.fetch_portfolio()

    def place_order(self, order):
        return self._inner.place_order(order)

    def get_order_state(self, client_id):
        return self._inner.get_order_state(client_id)

    def fetch_account(self) -> LiveAccountState:
        return LiveAccountState(
            equity=100_000.0,
            cash=0.0,
            settled_cash=0.0,
            available_funds=self.excess,
            buying_power=self.excess * 4,
            currency="USD",
            account_type="margin",
            excess_liquidity=self.excess,
            maintenance_margin=100_000.0 - self.excess,
            initial_margin=100_000.0 - self.excess,
            reported_type="margin",
        )


def _traders(world, excess: float):
    def open_trader(account):
        return _MarginTrader(world.traders(account), excess)

    return open_trader


def test_each_run_records_the_margin_cushion(world):
    _tick(world, DAY1, traders=_traders(world, 12_000.0))
    check = latest_check(world.state, world.live)
    assert check is not None
    assert (check.source, check.level) == ("tick", "warn")
    assert check.cushion == pytest.approx(0.12)
    alerts = world.state.sql(
        "SELECT dedupe_key FROM notification_outbox WHERE dedupe_key LIKE 'margin:%'"
    )
    assert [r["dedupe_key"] for r in alerts] == [f"margin:{world.live}:warn:" + _today()]


def test_a_dry_run_records_no_margin_check(world):
    _tick(world, DAY1, traders=_traders(world, 5_000.0), dry_run=True)
    assert recent_checks(world.state, world.live) == []


def test_a_cash_account_records_nothing(world):
    _tick(world, DAY1)
    assert recent_checks(world.state, world.live) == []


def _today() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).date().isoformat()


# ---- short book financing at the broker's borrow rate -----------------------------------------


class _ShortTrader(_MarginTrader):
    """The account holds the book's own short in DOWN.US, and the broker's
    borrow source quotes a 3.6 % yearly fee."""

    def fetch_portfolio(self):
        from dataclasses import replace

        held = self._inner.fetch_portfolio()
        return replace(held, positions={**held.positions, "DOWN.US": -10.0})

    def borrow_source(self, fees=None):
        from stonks.execution.borrow import BorrowQuote, FlatBorrow

        return FlatBorrow(overrides={"DOWN.US": BorrowQuote("easy", 0.036, None)})


def test_a_short_book_accrues_the_brokers_borrow_fee(world):
    from datetime import timedelta

    state = world.state
    state.execute("UPDATE portfolios SET allow_short = 1 WHERE id = ?", [world.live])
    state.execute(
        "INSERT INTO orders (client_id, ticker, side, quantity, order_type, status, created_at,"
        " updated_at, portfolio_id, position_effect) VALUES ('short-1', 'DOWN.US', 'sell', 10,"
        " 'market', 'filled', 'x', 'x', ?, 'open')",
        [world.live],
    )
    state.execute(
        "INSERT INTO fills (order_client_id, ticker, quantity, price, fee, filled_at,"
        " portfolio_id) VALUES ('short-1', 'DOWN.US', 10, 80, 0, ?, ?)",
        [f"{(DAY1 - timedelta(days=5)).isoformat()}T15:00:00+00:00", world.live],
    )
    start = DAY1 - timedelta(days=3)
    state.execute(
        "INSERT INTO financing_accruals (portfolio_id, accrued_through, updated_at)"
        " VALUES (?, ?, 'x')",
        [world.live, start.isoformat()],
    )

    def open_trader(account):
        return _ShortTrader(world.traders(account), 60_000.0)

    _tick(world, DAY1, traders=open_trader)
    rows = state.sql(
        "SELECT ticker, kind, amount, days FROM financing_charges WHERE portfolio_id = ?",
        [world.live],
    )
    assert [(r["ticker"], r["kind"], r["days"]) for r in rows] == [("DOWN.US", "borrow_fee", 3)]
    assert rows[0]["amount"] < 0
    through = state.sql(
        "SELECT accrued_through FROM financing_accruals WHERE portfolio_id = ?", [world.live]
    )
    assert through[0]["accrued_through"] == DAY1.isoformat()

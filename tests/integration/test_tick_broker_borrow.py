"""A short book at a real broker reads borrow from the broker (roadmap
19.14): the tick hands the short rules the broker's own locate, with the
lake's ``borrow_rates`` as the fee, instead of the settings' source."""

from __future__ import annotations

import pytest

import tests.integration.test_tick_modes as modes
from stonks.execution.borrow import BorrowSource, FlatBorrow, LakeBorrowSource
from stonks.production.rules._common import settings_of
from stonks.production.rules.borrow_check import BorrowCheck, borrow_source
from tests.integration.test_tick_tickets import DAY1, _tick

POLICY = '{"rules": {"borrow_check": {"enabled": true, "borrow": {"hard_fee_rate_annual": 0.04}}}}'


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
    w.state.execute(
        "UPDATE portfolios SET allow_short = 1, risk_policy_json = ? WHERE id = ?",
        [POLICY, w.live],
    )
    yield w
    w.state.close()


class _Locating:
    """A trader with IBKR's borrow capability."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.fees: list[BorrowSource | None] = []
        self.source = FlatBorrow()

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def fetch_portfolio(self):
        return self._inner.fetch_portfolio()

    def place_order(self, order):
        return self._inner.place_order(order)

    def get_order_state(self, client_id):
        return self._inner.get_order_state(client_id)

    def borrow_source(self, fees=None):
        self.fees.append(fees)
        return self.source


@pytest.fixture
def seen(monkeypatch):
    """The borrow source each ``borrow_check`` run used, by portfolio."""
    out: dict[str, object] = {}
    real = BorrowCheck.apply

    def spy(self, orders, ctx):
        out[ctx.portfolio_id] = borrow_source(ctx, settings_of(ctx.policy, "borrow_check").borrow)
        return real(self, orders, ctx)

    monkeypatch.setattr(BorrowCheck, "apply", spy)
    return out


def test_the_short_rules_read_the_brokers_locate_with_lake_fees(world, seen):
    traders: list[_Locating] = []

    def locating(account):
        trader = _Locating(world.traders(account))
        traders.append(trader)
        return trader

    _tick(world, DAY1, traders=locating)
    [trader] = traders
    assert seen[world.live] is trader.source
    [fees] = trader.fees
    assert isinstance(fees, LakeBorrowSource)


def test_an_account_that_cannot_short_keeps_the_settings_source(world, seen):
    def cash_account(account):
        trader = _Locating(world.traders(account))
        trader.source = None  # type: ignore[assignment]
        return trader

    _tick(world, DAY1, traders=cash_account)
    located = seen[world.live]
    _tick(world, DAY1)  # no locate capability at all
    assert type(located) is type(seen[world.live])
    assert not isinstance(located, LakeBorrowSource)

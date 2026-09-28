"""The submit window runs the 19.5 submit gate and the pre-open gap check
before it sends a ticket (roadmap 19.14)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

import tests.integration.test_tick_modes as modes
from stonks.config import RiskPolicy
from stonks.connections.providers.fake import fake_position
from stonks.core.clock import FakeClock
from stonks.execution.brokers.base import Quote
from stonks.production.live.checks import list_reports
from stonks.production.live.settings import LiveSettings
from stonks.production.submit import submit_tickets
from stonks.production.tickets import list_tickets
from tests.integration.test_tick_tickets import DAY1, IN_WINDOW, _tick

WINDOWED = replace(modes.SETTINGS, live=LiveSettings(submit_in_window=True))
BAND = RiskPolicy.model_validate({"rules": {"price_band": {"band_pct": 0.02, "max_gap_pct": 0.05}}})


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


class _Quoting:
    """A trader that also answers pre-open quotes (``QuoteSource``)."""

    def __init__(self, inner, quotes: dict[str, float]) -> None:
        self._inner, self._quotes = inner, quotes

    def __getattr__(self, name):
        return getattr(self._inner, name)

    # the capability checks look methods up on the class
    def fetch_portfolio(self):
        return self._inner.fetch_portfolio()

    def place_order(self, order):
        return self._inner.place_order(order)

    def get_order_state(self, client_id):
        return self._inner.get_order_state(client_id)

    def quotes(self, tickers):
        at = datetime(2026, 3, 18, 13, 0, tzinfo=UTC)
        return {
            t: Quote(ticker=t, last=self._quotes[t], bid=None, ask=None, as_of=at, delayed=True)
            for t in tickers
            if t in self._quotes
        }


def _submit(world, *, quotes=None, risk=None, live=None):
    from stonks.accounts import PortfolioRepository, Scope

    def open_broker(portfolio_id: str):
        account = PortfolioRepository(world.state).get(Scope.service("t"), portfolio_id)
        trader = world.traders(account)
        return _Quoting(trader, quotes) if quotes is not None else trader

    return submit_tickets(
        world.state, open_broker, clock=FakeClock(IN_WINDOW), risk=risk, live=live
    )


def _decision(world) -> float:
    [ticket] = list_tickets(world.state, portfolio_ids=[world.live])
    assert ticket.order.decision_price
    return float(ticket.order.decision_price)


# ---- the submit gate ---------------------------------------------------------------------


def test_the_gate_stores_a_submit_report_before_sending(world):
    _tick(world, DAY1, WINDOWED)
    result = _submit(world)
    assert result.sent == 1
    [report] = list_reports(world.state, portfolio_ids=[world.live])
    assert report.kind == "submit" and report.status == "clean"


def test_drift_at_the_gate_sends_nothing(world):
    _tick(world, DAY1, WINDOWED)
    [account] = world.book.accounts
    world.book.positions[account.id] = [fake_position("HAND", 5, 10.0)]
    result = _submit(world, live=LiveSettings(allow_manual_trades=False))
    assert result.sent == 0
    [pf] = result.portfolios
    assert pf.status == "skipped" and "drift" in (pf.reason or "")
    assert world.book.orders == {}
    assert list_tickets(world.state, portfolio_ids=[world.live])[0].status == "approved"


# ---- the pre-open gap check --------------------------------------------------------------


def test_an_opening_ticket_that_gapped_is_held_and_the_owner_alerted(world):
    _tick(world, DAY1, WINDOWED)
    moved = _decision(world) * 1.10
    result = _submit(world, quotes={"UP.US": moved}, risk=BAND)
    assert result.sent == 0
    [pf] = result.portfolios
    assert pf.held == 1 and pf.gap_held == (list_tickets(world.state)[0].id,)
    assert world.book.orders == {}
    assert list_tickets(world.state, portfolio_ids=[world.live])[0].status == "approved"
    [alert] = world.state.sql(
        "SELECT * FROM notification_outbox WHERE dedupe_key LIKE 'preopen_gap:%'"
    )
    assert alert["user_id"] == world.bob.user_id
    assert "moved" in alert["body"]


def test_a_small_move_is_sent(world):
    _tick(world, DAY1, WINDOWED)
    result = _submit(world, quotes={"UP.US": _decision(world) * 1.01}, risk=BAND)
    assert result.sent == 1 and result.portfolios[0].gap_held == ()


def test_an_opening_ticket_with_no_quote_is_held_while_the_band_is_on(world):
    _tick(world, DAY1, WINDOWED)
    result = _submit(world, risk=BAND)  # the fake trader answers no quotes
    assert result.sent == 0 and result.portfolios[0].held == 1


def test_the_gap_check_is_off_while_the_band_is_off(world):
    _tick(world, DAY1, WINDOWED)
    assert _submit(world, quotes={"UP.US": 1.0}).sent == 1


def test_the_portfolio_policy_tightens_the_band(world):
    world.state.execute(
        "UPDATE portfolios SET risk_policy_json = ? WHERE id = ?",
        ['{"rules": {"price_band": {"band_pct": 0.02, "max_gap_pct": 0.05}}}', world.live],
    )
    _tick(world, DAY1, WINDOWED)
    result = _submit(world, quotes={"UP.US": _decision(world) * 1.2}, risk=RiskPolicy())
    assert result.sent == 0 and result.portfolios[0].held == 1

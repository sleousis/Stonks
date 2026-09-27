"""Building a live book's context (roadmap 19.6, 19.7): the allocation,
the broker's account and quotes through its capabilities, today's sent
notional, and the account rules' inputs."""

from __future__ import annotations

from datetime import UTC, date, datetime

from stonks.accounts.rules import AccountProfile
from stonks.accounts.rules.profiles import set_profile
from stonks.core.types import Portfolio
from stonks.execution.brokers.base import LiveAccountState, Quote
from stonks.production.live.allocation import set_allocation
from stonks.production.live.context import build_live_context, sent_notional_today

DAY = date(2026, 9, 28)
NOW = datetime(2026, 9, 28, 13, 0, tzinfo=UTC)


class _Broker:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    def fetch_portfolio(self):
        return Portfolio(cash=0.0)

    def place_order(self, order):
        return None

    def reconcile(self):
        return []

    def fetch_account(self) -> LiveAccountState:
        if self.fail:
            raise RuntimeError("gateway down")
        return LiveAccountState(
            equity=5_000.0,
            cash=5_000.0,
            settled_cash=5_000.0,
            available_funds=5_000.0,
            buying_power=5_000.0,
            currency="USD",
            account_type="cash",
        )

    def quotes(self, tickers):
        if self.fail:
            raise RuntimeError("no data")
        return {
            t: Quote(t, last=10.0, bid=9.9, ask=10.1, as_of=NOW, delayed=False) for t in tickers
        }


def _order(state, cid, side, qty, limit, *, status="pending", effect=None, day=DAY):
    state.execute(
        "INSERT INTO orders (client_id, ticker, side, quantity, order_type, limit_price, status,"
        " created_at, updated_at, portfolio_id, position_effect)"
        " VALUES (?, 'A.US', ?, ?, 'limit', ?, ?, ?, 'x', 'pf_default', ?)",
        [cid, side, qty, limit, status, f"{day.isoformat()}T13:00:00+00:00", effect],
    )


def test_sent_notional_counts_todays_live_opening_orders(state):
    _order(state, "a", "buy", 10.0, 20.0)
    _order(state, "b", "buy", 5.0, 20.0, status="rejected")
    _order(state, "c", "sell", 3.0, 20.0)  # a close
    _order(state, "d", "sell", 2.0, 50.0, effect="open")  # a short sale
    _order(state, "e", "buy", 1.0, 20.0, day=date(2026, 9, 25))
    assert sent_notional_today(state, DAY, portfolio_ids=["pf_default"]) == 300.0
    assert sent_notional_today(state, DAY) == 300.0
    assert sent_notional_today(state, DAY, portfolio_ids=["pf_other"]) == 0.0


def test_build_reads_the_broker_and_the_state(state):
    set_allocation(state, "pf_default", 2_000.0, currency="USD", actor="user:u", reason="start")
    set_profile(state, AccountProfile("pf_default", "us"), actor="user:u")
    _order(state, "a", "buy", 10.0, 20.0)
    live = build_live_context(state, "pf_default", DAY, broker=_Broker(), tickers=["A.US"])
    assert live.allocation == 2_000.0
    assert live.account is not None and live.account.equity == 5_000.0
    assert set(live.quotes) == {"A.US"}
    assert live.sent_today == live.sent_today_user == live.sent_today_global == 200.0
    assert live.account_rules is not None and live.account_rules.account is live.account


def test_a_failing_broker_leaves_the_account_and_quotes_empty(state):
    live = build_live_context(state, "pf_default", DAY, broker=_Broker(fail=True), tickers=["A"])
    assert live.account is None and live.quotes == {}
    assert live.allocation is None and live.account_rules is None


def test_a_broker_without_capabilities_is_fine(state):
    live = build_live_context(state, "pf_default", DAY, broker=object())
    assert live.account is None and live.quotes == {}

"""The simulated intraday broker: next-bar fills on minute bars (roadmap 21.2.3)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from stonks.backtest.costs import FixedCostModel
from stonks.backtest.fills import MinuteFillSettings
from stonks.core.clock import FakeClock
from stonks.core.interval import Interval
from stonks.core.stream import QuoteTick, StreamBar
from stonks.core.types import Order, Portfolio
from stonks.engine.driver import BarClose
from stonks.engine.sim_broker import IntradaySimBroker, calendar_session_key
from stonks.execution.brokers.base import (
    ExecutionSource,
    OrderCanceller,
    OrderStateSource,
)

# Monday 2026-09-28, 09:30 New York
M0 = datetime(2026, 9, 28, 13, 30, tzinfo=UTC)
ONE = timedelta(minutes=1)


def sbar(minute: int, ticker: str = "A.US", open_: float = 10.0, volume: int = 10_000,
         start: datetime = M0) -> StreamBar:  # fmt: skip
    ts = start + minute * ONE
    return StreamBar(ticker, ts, Interval.MIN_1, open_, open_ + 0.5, open_ - 0.5, open_, volume)


_seq = iter(range(1, 10_000))


def close_of(*bars: StreamBar) -> BarClose:
    """The bar close event the driver sends for bars that share a start."""
    return BarClose(
        at=bars[0].timestamp + ONE, interval=Interval.MIN_1, bars=bars, sequence=next(_seq)
    )


def buy(qty: float = 100.0, cid: str = "c1", ticker: str = "A.US", **kw) -> Order:
    return Order(client_id=cid, ticker=ticker, side="buy", quantity=qty, **kw)


def same_day(ticker: str, at: datetime) -> date:
    return at.date()


def make(cash: float = 1_000_000.0, clock_at: datetime | None = None, **kw):
    clock = FakeClock(clock_at or M0 + ONE + timedelta(seconds=2))
    kw.setdefault("session_key", same_day)
    broker = IntradaySimBroker(Portfolio(cash=cash, positions={}), clock=clock, **kw)
    return broker, clock


def test_has_the_capabilities_reconciliation_reads():
    broker, _ = make()
    assert isinstance(broker, ExecutionSource)
    assert isinstance(broker, OrderStateSource)
    assert isinstance(broker, OrderCanceller)


def test_place_does_not_fill_and_the_order_is_working():
    broker, _ = make()
    assert broker.place_order(buy(decided_at=M0 + ONE)) is None
    state = broker.get_order_state("c1")
    assert state is not None
    assert (state.state, state.filled_quantity, state.avg_fill_price) == ("accepted", 0.0, None)
    assert broker.executions(M0 - ONE) == []


def test_decision_on_close_of_minute_t_fills_at_the_open_of_minute_t_plus_one():
    broker, _ = make()
    broker.place_order(buy(decided_at=M0 + ONE))  # decided on the close of minute 0
    new = broker.on_bar_close(close_of(sbar(1, open_=10.2)))
    assert len(new) == 1
    ex = new[0]
    assert (ex.client_id, ex.quantity, ex.price) == ("c1", 100.0, pytest.approx(10.2))
    assert ex.executed_at == M0 + ONE  # the open of minute 1
    state = broker.get_order_state("c1")
    assert state is not None
    assert (state.state, state.status, state.filled_quantity) == ("filled", "filled", 100.0)
    assert broker.fetch_portfolio().positions["A.US"] == pytest.approx(100.0)


def test_a_bar_that_began_before_the_decision_bar_closed_is_not_used():
    broker, _ = make()
    broker.place_order(buy(decided_at=M0 + 2 * ONE))
    assert broker.on_bar_close(close_of(sbar(1))) == []
    assert len(broker.on_bar_close(close_of(sbar(2)))) == 1


def test_without_a_decision_time_the_clock_decides():
    broker, clock = make(clock_at=M0 + ONE + timedelta(seconds=2))
    broker.place_order(buy())
    assert broker.on_bar_close(close_of(sbar(0))) == []
    assert len(broker.on_bar_close(close_of(sbar(1)))) == 1


def test_costs_come_from_the_cost_model_and_become_the_commission():
    broker, _ = make(cost_model=FixedCostModel(slippage_bps=10.0, fee_per_trade=1.5))
    broker.place_order(buy(decided_at=M0 + ONE))
    (ex,) = broker.on_bar_close(close_of(sbar(1, open_=10.0)))
    assert ex.price == pytest.approx(10.01)
    assert ex.commission == pytest.approx(1.5)


def test_participation_cap_fills_across_minutes_with_distinct_execution_ids():
    broker, _ = make()
    broker.place_order(buy(qty=1_500.0, decided_at=M0 + ONE))
    first = broker.on_bar_close(close_of(sbar(1, volume=10_000, open_=10.0)))
    assert first[0].quantity == pytest.approx(1_000.0)
    state = broker.get_order_state("c1")
    assert state is not None and state.state == "partially_filled"
    second = broker.on_bar_close(close_of(sbar(2, volume=10_000, open_=11.0)))
    assert second[0].quantity == pytest.approx(500.0)
    assert first[0].broker_exec_id != second[0].broker_exec_id
    state = broker.get_order_state("c1")
    assert state is not None
    assert state.state == "filled"
    assert state.filled_quantity == pytest.approx(1_500.0)
    assert state.avg_fill_price == pytest.approx((1_000 * 10.0 + 500 * 11.0) / 1_500)
    assert len(broker.executions(M0)) == 2


def test_rest_of_a_day_order_expires_in_a_new_session():
    broker, _ = make()
    broker.place_order(buy(qty=1_500.0, decided_at=M0 + ONE))
    broker.on_bar_close(close_of(sbar(1)))
    tomorrow = M0 + timedelta(days=1)
    assert broker.on_bar_close(close_of(sbar(0, start=tomorrow))) == []
    state = broker.get_order_state("c1")
    assert state is not None
    assert (state.state, state.status) == ("expired", "cancelled")
    assert state.filled_quantity == pytest.approx(1_000.0)


def test_calendar_session_key_splits_days_and_leaves_the_night_out():
    key = calendar_session_key
    assert key("A.US", M0) == date(2026, 9, 28)
    assert key("A.US", M0 + timedelta(days=1)) == date(2026, 9, 29)
    assert key("A.US", M0 - timedelta(hours=2)) is None  # pre-market


def test_default_session_key_uses_the_exchange_calendar():
    broker = IntradaySimBroker(Portfolio(cash=1e6, positions={}), clock=FakeClock(M0))
    broker.place_order(buy(qty=1_500.0, decided_at=M0 + ONE))
    broker.on_bar_close(close_of(sbar(1)))
    broker.on_bar_close(close_of(sbar(0, start=M0 + timedelta(days=1))))
    state = broker.get_order_state("c1")
    assert state is not None and state.state == "expired"


def test_zero_volume_minute_keeps_the_order_working():
    broker, _ = make()
    broker.place_order(buy(decided_at=M0 + ONE))
    assert broker.on_bar_close(close_of(sbar(1, volume=0))) == []
    state = broker.get_order_state("c1")
    assert state is not None and state.state == "accepted"
    assert len(broker.on_bar_close(close_of(sbar(2)))) == 1


def test_another_tickers_bar_leaves_the_order_working():
    broker, _ = make()
    broker.place_order(buy(decided_at=M0 + ONE))
    assert broker.on_bar_close(close_of(sbar(1, ticker="B.US"))) == []
    assert len(broker.on_bar_close(close_of(sbar(2), sbar(2, ticker="B.US")))) == 1


def test_gap_beyond_the_limit_expires():
    broker, _ = make(fill=MinuteFillSettings(max_gap_bars=2))
    broker.place_order(buy(decided_at=M0 + ONE))
    assert broker.on_bar_close(close_of(sbar(4))) == []
    state = broker.get_order_state("c1")
    assert state is not None and state.state == "expired"


def test_recorded_quote_adds_the_half_spread():
    broker, _ = make()
    broker.on_quote(QuoteTick("A.US", M0 + ONE - timedelta(seconds=1), bid=9.98, ask=10.02))
    broker.place_order(buy(decided_at=M0 + ONE))
    (ex,) = broker.on_bar_close(close_of(sbar(1, open_=10.0)))
    assert ex.price == pytest.approx(10.02)


def test_a_quote_after_the_fill_bar_opened_is_not_used():
    broker, _ = make()
    broker.on_quote(QuoteTick("A.US", M0 + ONE + timedelta(seconds=5), bid=9.0, ask=11.0))
    broker.place_order(buy(decided_at=M0 + ONE))
    (ex,) = broker.on_bar_close(close_of(sbar(1, open_=10.0)))
    assert ex.price == pytest.approx(10.0)


def test_a_later_quote_does_not_hide_the_one_before_the_open():
    """Live, quotes keep arriving while the fill bar forms. The fill still
    takes the half spread of the last quote before that bar opened."""
    broker, _ = make()
    broker.on_quote(QuoteTick("A.US", M0 + ONE - timedelta(seconds=1), bid=9.98, ask=10.02))
    broker.on_quote(QuoteTick("A.US", M0 + ONE + timedelta(seconds=30), bid=9.9, ask=10.1))
    broker.place_order(buy(decided_at=M0 + ONE))
    (ex,) = broker.on_bar_close(close_of(sbar(1, open_=10.0)))
    assert ex.price == pytest.approx(10.02)


def test_buy_beyond_the_cash_fills_what_it_can_and_expires_the_rest():
    broker, _ = make(cash=505.0)
    broker.place_order(buy(qty=100.0, decided_at=M0 + ONE))
    (ex,) = broker.on_bar_close(close_of(sbar(1, open_=10.0)))
    assert ex.quantity == pytest.approx(50.5)
    state = broker.get_order_state("c1")
    assert state is not None and state.state == "expired"


def test_place_is_idempotent_by_client_id():
    broker, _ = make()
    broker.place_order(buy(decided_at=M0 + ONE))
    broker.place_order(buy(qty=999.0, decided_at=M0 + ONE))
    (ex,) = broker.on_bar_close(close_of(sbar(1)))
    assert ex.quantity == 100.0


def test_cancel_stops_a_working_order():
    broker, _ = make()
    broker.place_order(buy(decided_at=M0 + ONE))
    assert broker.cancel_order("c1") is True
    assert broker.cancel_order("c1") is False
    assert broker.cancel_order("nope") is False
    assert broker.on_bar_close(close_of(sbar(1))) == []
    state = broker.get_order_state("c1")
    assert state is not None and state.state == "cancelled"


def test_expire_open_ends_every_working_order():
    broker, _ = make()
    broker.place_order(buy(decided_at=M0 + ONE))
    broker.place_order(buy(cid="c2", ticker="B.US", decided_at=M0 + ONE))
    assert broker.expire_open() == ["c1", "c2"]
    assert broker.expire_open() == []


def test_unknown_client_id_has_no_state():
    broker, _ = make()
    assert broker.get_order_state("nope") is None


def test_reconcile_returns_every_fill_for_the_broker_protocol():
    broker, _ = make()
    broker.place_order(buy(decided_at=M0 + ONE))
    broker.on_bar_close(close_of(sbar(1)))
    (fill,) = broker.reconcile()
    assert (fill.order_client_id, fill.quantity) == ("c1", 100.0)
    assert fill.broker_exec_id is not None

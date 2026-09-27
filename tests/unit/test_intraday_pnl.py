"""Live marks and intraday P&L (roadmap 21.3.3): the mark book and the
per-book position ledger, pure and without a database."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from stonks.core.interval import Interval
from stonks.core.stream import Heartbeat, QuoteTick, StreamBar, TradeTick
from stonks.engine.driver import BarClose
from stonks.production.intraday_pnl import (
    DayFill,
    MarkBook,
    PositionLedger,
    drawdown_from_high,
)

T0 = datetime(2026, 9, 28, 13, 30, tzinfo=UTC)
ONE = timedelta(minutes=1)


def fill(ticker: str, side: str, qty: float, price: float, fee: float = 0.0, n: int = 1) -> DayFill:
    return DayFill(
        fill_id=n,
        ticker=ticker,
        side=side,  # type: ignore[arg-type]
        quantity=qty,
        price=price,
        fee=fee,
        filled_at=T0 + n * ONE,
        strategy_id=None,
    )


# ---- the mark book ---------------------------------------------------------------


def test_trades_quotes_and_bars_set_the_mark():
    book = MarkBook()
    book(TradeTick("A.US", T0, 10.0, 5))
    assert book.price("A.US") == 10.0
    book(QuoteTick("A.US", T0 + ONE, bid=10.9, ask=11.1))
    assert book.price("A.US") == pytest.approx(11.0)
    bar = StreamBar("A.US", T0 + ONE, Interval.MIN_1, 11, 12, 10, 11.5, 100)
    book(bar)
    mark = book.get("A.US")
    assert mark is not None and mark.price == 11.5
    assert mark.at == T0 + 2 * ONE and mark.source == "bar"


def test_an_older_event_never_moves_the_mark_back():
    book = MarkBook()
    book(TradeTick("A.US", T0 + ONE, 11.0))
    assert book.update("A.US", 9.0, T0, "trade") is False
    book(TradeTick("A.US", T0, 9.0))
    assert book.price("A.US") == 11.0


def test_delayed_or_unsound_quotes_and_heartbeats_are_ignored():
    book = MarkBook()
    book(QuoteTick("A.US", T0, bid=10.0, ask=10.2, delayed=True))
    book(QuoteTick("B.US", T0, bid=10.3, ask=10.1))  # crossed, no mid
    book(Heartbeat(T0))
    assert book.marks() == {}


def test_a_bar_close_event_marks_every_bar():
    book = MarkBook()
    bars = (
        StreamBar("A.US", T0, Interval.MIN_1, 1, 2, 1, 2, 1),
        StreamBar("B.US", T0, Interval.MIN_1, 5, 6, 4, 4.5, 1),
    )
    book.on_bar_close(BarClose(at=T0 + ONE, interval=Interval.MIN_1, bars=bars, sequence=1))
    assert book.marks() == {"A.US": 2.0, "B.US": 4.5}
    assert book.age("A.US", T0 + 3 * ONE) == timedelta(minutes=2)
    assert book.age("Z.US", T0) is None


def test_a_bad_price_is_refused():
    book = MarkBook()
    assert book.update("A.US", 0.0, T0, "trade") is False
    assert book.update("A.US", float("nan"), T0, "trade") is False
    assert book.marks() == {}


# ---- the position ledger ---------------------------------------------------------


def test_held_overnight_is_marked_against_the_prior_close():
    ledger = PositionLedger({"A.US": 10}, {"A.US": 100.0})
    marked = ledger.mark({"A.US": 103.0})
    assert ledger.start_value() == 1000.0
    assert marked.unrealised == pytest.approx(30.0)
    assert ledger.realised == 0.0 and marked.exposures == {"A.US": pytest.approx(1030.0)}


def test_a_round_trip_realises_price_minus_cost_less_fees():
    ledger = PositionLedger({}, {})
    ledger.apply(fill("A.US", "buy", 10, 100.0, fee=1.0, n=1))
    ledger.apply(fill("A.US", "sell", 4, 105.0, fee=0.5, n=2))
    assert ledger.realised == pytest.approx(20.0)
    assert ledger.fees == pytest.approx(1.5)
    marked = ledger.mark({"A.US": 102.0})
    assert marked.unrealised == pytest.approx(12.0)  # 6 left at cost 100
    assert ledger.fills == 2


def test_adding_to_a_position_averages_the_cost():
    ledger = PositionLedger({"A.US": 10}, {"A.US": 100.0})
    ledger.apply(fill("A.US", "buy", 10, 110.0))
    ledger.apply(fill("A.US", "sell", 20, 120.0, n=2))
    # average 105, 20 shares sold at 120
    assert ledger.realised == pytest.approx(300.0)
    assert ledger.positions() == {}


def test_a_sell_through_zero_opens_a_short_at_the_fill_price():
    ledger = PositionLedger({"A.US": 5}, {"A.US": 100.0})
    ledger.apply(fill("A.US", "sell", 8, 90.0))
    assert ledger.realised == pytest.approx(-50.0)
    assert ledger.positions() == {"A.US": pytest.approx(-3.0)}
    marked = ledger.mark({"A.US": 85.0})
    assert marked.unrealised == pytest.approx(15.0)  # short 3 from 90
    assert marked.exposures == {"A.US": pytest.approx(-255.0)}


def test_covering_a_short_realises_the_drop():
    ledger = PositionLedger({"A.US": -10}, {"A.US": 50.0})
    ledger.apply(fill("A.US", "buy", 10, 45.0))
    assert ledger.realised == pytest.approx(50.0)


def test_day_pnl_equals_the_change_in_value():
    """Realised plus unrealised less fees is the value change with cash."""
    start = {"A.US": 10.0, "B.US": -5.0}
    ref = {"A.US": 100.0, "B.US": 20.0}
    ledger = PositionLedger(start, ref)
    trades = [
        fill("A.US", "sell", 15, 101.0, fee=1.0, n=1),
        fill("B.US", "buy", 8, 19.0, fee=0.2, n=2),
        fill("C.US", "buy", 3, 7.0, n=3),
    ]
    cash = 0.0
    for f in trades:
        ledger.apply(f)
        sign = -1 if f.side == "buy" else 1
        cash += sign * f.quantity * f.price - f.fee
    marks = {"A.US": 99.0, "B.US": 18.5, "C.US": 7.5}
    marked = ledger.mark(marks)
    end_value = cash + sum(q * marks[t] for t, q in ledger.positions().items())
    start_value = sum(q * ref[t] for t, q in start.items())
    pnl = ledger.realised + marked.unrealised - ledger.fees
    assert pnl == pytest.approx(end_value - start_value)


def test_a_held_name_without_a_prior_close_is_priced_at_its_first_mark():
    ledger = PositionLedger({"A.US": 10}, {})
    first = ledger.mark({})
    assert first.unmarked == ("A.US",) and first.unrealised == 0.0
    assert ledger.start_value() == 0.0
    ledger.mark({"A.US": 50.0})
    assert ledger.start_value() == 500.0
    assert ledger.mark({"A.US": 52.0}).unrealised == pytest.approx(20.0)


def test_an_unmarked_trade_is_valued_at_its_cost():
    ledger = PositionLedger({}, {})
    ledger.apply(fill("A.US", "buy", 2, 10.0))
    marked = ledger.mark({})
    assert marked.unrealised == 0.0 and marked.unmarked == ("A.US",)
    assert marked.exposures == {"A.US": pytest.approx(20.0)}


def test_drawdown_from_the_days_high():
    assert drawdown_from_high(pnl=-10.0, high=0.0, base=1000.0) == pytest.approx(-0.01)
    assert drawdown_from_high(pnl=40.0, high=100.0, base=1000.0) == pytest.approx(-60 / 1100)
    assert drawdown_from_high(pnl=5.0, high=5.0, base=1000.0) == 0.0
    assert drawdown_from_high(pnl=-5.0, high=0.0, base=0.0) == 0.0

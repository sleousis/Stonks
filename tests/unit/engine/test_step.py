"""The decision step: decide on a bar close (roadmap 21.2.2)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from stonks.core.interval import Interval
from stonks.core.stream import StreamBar
from stonks.core.types import Portfolio
from stonks.engine.driver import BarClose
from stonks.engine.sessions import FLATTEN_STRATEGY_ID, HaltTable, SessionRules, TradingHalt
from stonks.engine.step import DecisionStep, StepBook, StepDecision
from stonks.portfolio.settings import ConstructionSettings
from tests.unit.engine.minute_lake import (
    DAYS,
    TICKERS,
    MinuteMomentum,
    minute_lake,
    session_minutes,
)

ONE = timedelta(minutes=1)
MINUTES = session_minutes(DAYS[0])


def close_event(minute: int, seq: int = 1, day: int = 0) -> BarClose:
    """The bar close of both tickers for the bar ``minute`` after the open."""
    start = session_minutes(DAYS[day])[minute].replace(tzinfo=UTC)
    close = 100.0 + minute / 100
    bars = tuple(
        StreamBar(t, start, Interval.MIN_1, close, close + 0.5, close - 0.5, close, 1_000)
        for t in TICKERS
    )
    return BarClose(at=start + ONE, interval=Interval.MIN_1, bars=bars, sequence=seq)


@pytest.fixture(scope="module")
def lake():
    lk = minute_lake()
    yield lk
    lk.close()


def make_step(lake, books, portfolios=None, **kw):
    portfolios = portfolios or {b.id: Portfolio(cash=10_000.0) for b in books}
    decisions: list[StepDecision] = []
    step = DecisionStep(
        {"mom": MinuteMomentum({"lookback": 5})},
        books,
        lake,
        universe=TICKERS,
        portfolio=lambda book_id: portfolios[book_id],
        on_decision=decisions.append,
        **kw,
    )
    return step, decisions, portfolios


def test_strategies_see_only_bars_closed_by_the_event(lake) -> None:
    step, _, _ = make_step(lake, [StepBook(id="b")])
    strategy = step.strategies["mom"]
    event = close_event(30)
    step.on_bar_close(event)
    decision_bar = event.at.replace(tzinfo=None) - ONE
    assert strategy.seen, "the strategy was asked"
    for as_of, _ticker, last in strategy.seen:
        assert as_of == decision_bar
        assert last == decision_bar  # the bar that just closed, nothing later


def test_signals_are_scored_once_per_bar_for_every_book(lake) -> None:
    books = [StepBook(id="a"), StepBook(id="b", portfolio_id="pf_b")]
    step, decisions, _ = make_step(lake, books)
    step.on_bar_close(close_event(30))
    strategy = step.strategies["mom"]
    assert len(strategy.seen) == len(TICKERS)
    assert [d.book_id for d in decisions] == ["a", "b"]


def test_orders_carry_book_decision_stamp_and_price(lake) -> None:
    books = [StepBook(id="a"), StepBook(id="b", portfolio_id="pf_b")]
    step, decisions, _ = make_step(lake, books)
    step.on_bar_close(close_event(30))
    a, b = decisions
    assert a.orders and b.orders
    stamp = (close_event(30).at - ONE).replace(tzinfo=None).isoformat()
    for order in a.orders:
        assert order.client_id.startswith(f"mom:{stamp}:")
        assert order.strategy_id == "mom"
        assert order.portfolio_id is None
        assert order.decision_price == pytest.approx(step.marks[order.ticker])
        assert order.decided_at == close_event(30).at
    for order in b.orders:
        assert order.client_id.startswith(f"pf_b:mom:{stamp}:")
        assert order.portfolio_id == "pf_b"


def test_marks_carry_forward_per_ticker(lake) -> None:
    step, _, _ = make_step(lake, [StepBook(id="a")])
    step.on_bar_close(close_event(30))
    one = close_event(31, seq=2)
    only_a = BarClose(at=one.at, interval=one.interval, bars=one.bars[:1], sequence=2)
    step.on_bar_close(only_a)
    assert step.marks[TICKERS[0]] == pytest.approx(100.31)
    assert step.marks[TICKERS[1]] == pytest.approx(100.30)


def test_opening_edge_blocks_entries(lake) -> None:
    book = StepBook(id="a", sessions=SessionRules(entry_delay_minutes=60))
    step, decisions, _ = make_step(lake, [book])
    step.on_bar_close(close_event(30))
    (decision,) = decisions
    assert decision.orders == []
    assert decision.dropped and {d.reason for d in decision.dropped} == {"opening"}


def test_regular_hours_keep_entries(lake) -> None:
    book = StepBook(id="a", sessions=SessionRules(entry_delay_minutes=5))
    step, decisions, _ = make_step(lake, [book])
    step.on_bar_close(close_event(30))
    assert decisions[0].orders and not decisions[0].dropped


def test_a_halted_ticker_gets_no_orders(lake) -> None:
    at = close_event(30).at
    halts = HaltTable([TradingHalt(t, at - ONE, None, "luld") for t in TICKERS])
    book = StepBook(id="a", sessions=SessionRules())
    step, decisions, _ = make_step(lake, [book], halts=halts)
    step.on_bar_close(close_event(30))
    assert decisions[0].orders == []
    assert {d.reason for d in decisions[0].dropped} == {"halted"}


def test_flatten_window_closes_every_position(lake) -> None:
    rules = SessionRules(flatten_at_close=True, flatten_minutes=5)
    book = StepBook(id="a", sessions=rules)
    held = {"a": Portfolio(cash=0.0, positions={TICKERS[0]: 10.0, TICKERS[1]: 4.0})}
    step, decisions, _ = make_step(lake, [book], portfolios=held)
    step.on_bar_close(close_event(len(MINUTES) - 3))  # closes 2 minutes before 20:00
    orders = decisions[0].orders
    assert sorted((o.ticker, o.side, o.quantity) for o in orders) == [
        (TICKERS[0], "sell", 10.0),
        (TICKERS[1], "sell", 4.0),
    ]
    assert all(o.strategy_id == FLATTEN_STRATEGY_ID for o in orders)
    assert sorted(decisions[0].flattened) == sorted(TICKERS)


def test_the_closing_bar_decides_nothing_under_session_rules(lake) -> None:
    book = StepBook(id="a", sessions=SessionRules())
    step, decisions, _ = make_step(lake, [book])
    step.on_bar_close(close_event(len(MINUTES) - 1))  # closes at 20:00
    assert decisions[0].orders == []


def test_without_session_rules_nothing_is_gated(lake) -> None:
    step, decisions, _ = make_step(lake, [StepBook(id="a")])
    step.on_bar_close(close_event(len(MINUTES) - 1))
    assert decisions[0].dropped == []


def test_exit_owner_comes_from_recorded_fills(lake) -> None:
    held = {"a": Portfolio(cash=0.0, positions={TICKERS[0]: 5.0})}

    class Never(MinuteMomentum):
        def estimate_return(self, ticker, as_of, lake):
            return None

    decisions: list[StepDecision] = []
    step = DecisionStep(
        {"mom": Never()},
        [StepBook(id="a")],
        lake,
        universe=TICKERS,
        portfolio=lambda _: held["a"],
        on_decision=decisions.append,
    )
    step.on_bar_close(close_event(30))
    assert decisions[0].result is not None
    assert decisions[0].result.reason == "no_active_owner"
    step.record_fill("a", TICKERS[0], "mom")
    step.on_bar_close(close_event(31, seq=2))
    assert [(o.ticker, o.side) for o in decisions[1].orders] == [(TICKERS[0], "sell")]


def test_a_constructor_book_uses_target_weights(lake) -> None:
    book = StepBook(id="a", construction=ConstructionSettings(method="equal_weight_top_n"))
    step, decisions, _ = make_step(lake, [book])
    step.on_bar_close(close_event(30))
    (decision,) = decisions
    assert decision.result is not None
    assert decision.result.target_book.weights
    assert all(o.side == "buy" for o in decision.orders)


def test_a_book_that_cannot_short_drops_negative_scores(lake) -> None:
    class Shorty(MinuteMomentum):
        supports_short = True

        def estimate_return(self, ticker, as_of, lake):
            return -0.5

    decisions: list[StepDecision] = []
    step = DecisionStep(
        {"s": Shorty()},
        [StepBook(id="a")],
        lake,
        universe=TICKERS,
        portfolio=lambda _: Portfolio(cash=1_000.0),
        on_decision=decisions.append,
    )
    step.on_bar_close(close_event(30))
    assert decisions[0].orders == []


def test_stale_tickers_cannot_open(lake) -> None:
    step, decisions, _ = make_step(lake, [StepBook(id="a")], stale_after=timedelta(minutes=2))
    first = close_event(30)
    only_a = BarClose(at=first.at, interval=first.interval, bars=first.bars[:1], sequence=1)
    step.on_bar_close(only_a)
    later = close_event(40, seq=2)
    step.on_bar_close(BarClose(later.at, later.interval, later.bars[1:], 2))
    assert all(o.ticker != TICKERS[0] for o in decisions[1].orders)


def test_step_is_a_driver_handler() -> None:
    from stonks.engine.driver import BarCloseHandler

    assert issubclass(DecisionStep, BarCloseHandler) or hasattr(DecisionStep, "on_bar_close")
    assert DecisionStep.name == "decision_step"


def test_needs_a_book_and_a_strategy(lake) -> None:
    with pytest.raises(ValueError, match="book"):
        DecisionStep({"m": MinuteMomentum()}, [], lake, universe=TICKERS, portfolio=Portfolio)
    with pytest.raises(ValueError, match="strateg"):
        DecisionStep({}, [StepBook(id="a")], lake, universe=TICKERS, portfolio=Portfolio)
    with pytest.raises(ValueError, match="unique"):
        DecisionStep(
            {"m": MinuteMomentum()},
            [StepBook(id="a"), StepBook(id="a")],
            lake,
            universe=TICKERS,
            portfolio=Portfolio,
        )


def test_other_intervals_are_ignored(lake) -> None:
    step, decisions, _ = make_step(lake, [StepBook(id="a")])
    event = close_event(30)
    five = BarClose(event.at, Interval.MIN_5, event.bars, 1)
    assert step.on_bar_close(five) == []
    assert decisions == []


def test_decision_time_is_aware_utc(lake) -> None:
    step, decisions, _ = make_step(lake, [StepBook(id="a")])
    step.on_bar_close(close_event(30))
    assert decisions[0].at.tzinfo is not None
    assert decisions[0].as_of.tzinfo is None
    assert decisions[0].as_of == datetime(2026, 9, 24, 14, 0)

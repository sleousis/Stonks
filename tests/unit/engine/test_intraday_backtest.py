"""The intraday backtest on the event driver (roadmap 21.2.2)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from stonks.backtest.engine import BacktestConfig, Backtester
from stonks.backtest.intraday import IntradayBacktestConfig, IntradayBacktester
from stonks.backtest.simulated_broker import SimulatedBroker
from stonks.core.interval import Interval
from stonks.core.types import Portfolio
from stonks.engine.sessions import SessionRules
from tests.unit.engine.minute_lake import (
    DAYS,
    TICKERS,
    MinuteMomentum,
    minute_frame,
    minute_lake,
    session_minutes,
)

ONE = timedelta(minutes=1)


def _utc(when: datetime) -> datetime:
    return when.replace(tzinfo=UTC) if when.tzinfo is None else when.astimezone(UTC)


@pytest.fixture(scope="module")
def lake():
    lk = minute_lake()
    yield lk
    lk.close()


def broker() -> SimulatedBroker:
    return SimulatedBroker(Portfolio(cash=100_000.0), slippage_bps=2.0, fee_per_trade=1.0)


def run_intraday(lake, construction="single_winner", **kw):
    b = broker()
    strategy = MinuteMomentum({"lookback": 10})
    config = IntradayBacktestConfig(
        start=DAYS[0], end=DAYS[-1], universe=TICKERS, construction=construction, **kw
    )
    bt = IntradayBacktester([strategy], b, lake, config)
    return bt, bt.run(), b, strategy


def run_bars(lake, construction="single_winner"):
    b = broker()
    config = BacktestConfig(
        start=DAYS[0],
        end=DAYS[-1],
        universe=TICKERS,
        interval=Interval.MIN_1,
        construction=construction,
    )
    report = Backtester([MinuteMomentum({"lookback": 10})], b, lake, config).run()
    return report, b


def fills(b: SimulatedBroker) -> list[tuple]:
    return [
        (f.order_client_id, f.ticker, f.side, round(f.quantity, 9), round(f.price, 9), f.filled_at)
        for f in b.fills
    ]


@pytest.mark.parametrize("construction", ["single_winner", "equal_weight_top_n"])
def test_parity_with_the_bar_backtester(lake, construction) -> None:
    _, intraday, ib, _ = run_intraday(lake, construction)
    bars, bb = run_bars(lake, construction)
    assert len(bb.fills) > 20, "the toy strategy trades"
    assert fills(ib) == fills(bb)
    assert intraday.equity_dates == bars.equity_dates
    assert intraday.equity_curve == pytest.approx(bars.equity_curve, rel=1e-12)
    assert intraday.sharpe == pytest.approx(bars.sharpe)
    assert intraday.periods_per_year == bars.periods_per_year


def test_every_bar_close_is_one_decision(lake) -> None:
    bt, report, _, _ = run_intraday(lake)
    n = sum(len(session_minutes(d)) for d in DAYS)
    assert len(report.equity_curve) == n
    assert len(bt.decisions) == n
    assert bt.driver_stats is not None
    assert bt.driver_stats.late_bars == 0
    assert bt.driver_stats.total_handler_errors == 0


def test_strategies_never_see_a_bar_that_has_not_closed(lake) -> None:
    _, _, _, strategy = run_intraday(lake)
    assert strategy.seen
    for as_of, _ticker, last in strategy.seen:
        assert last is not None and last <= as_of


def test_changing_the_future_does_not_change_the_past() -> None:
    cut = datetime.combine(DAYS[0], datetime.min.time()) + timedelta(hours=17)
    cut_utc = cut.replace(tzinfo=UTC)
    frame = minute_frame()
    changed = frame.copy()
    later = changed["timestamp"] >= pd.Timestamp(cut)
    for col in ("open", "high", "low", "close", "adj_close"):
        changed.loc[later, col] = changed.loc[later, col] * 3.0
    a, b = minute_lake(frame), minute_lake(changed)
    try:
        _, _, ab, _ = run_intraday(a)
        _, _, bb, _ = run_intraday(b)
    finally:
        a.close()
        b.close()
    before = [f for f in fills(ab) if _utc(f[5]) < cut_utc]
    assert before, "fills before the cut"
    assert before == [f for f in fills(bb) if _utc(f[5]) < cut_utc]


def test_runs_are_deterministic(lake) -> None:
    _, first, fb, _ = run_intraday(lake)
    _, second, sb, _ = run_intraday(lake)
    assert fills(fb) == fills(sb)
    assert first.equity_curve == second.equity_curve


def test_row_order_in_the_lake_does_not_matter(lake) -> None:
    shuffled = minute_lake(minute_frame().sample(frac=1.0, random_state=7))
    try:
        _, _, a, _ = run_intraday(lake)
        _, _, b, _ = run_intraday(shuffled)
    finally:
        shuffled.close()
    assert fills(a) == fills(b)


def test_orders_fill_at_the_next_bar_open(lake) -> None:
    bt, _, b, _ = run_intraday(lake)
    decided = {o.client_id: d.as_of for d in bt.decisions for o in d.orders}
    for fill in b.fills:
        root = fill.order_client_id.split("~")[0]
        assert _utc(fill.filled_at) > decided[root].replace(tzinfo=UTC)


def test_session_rules_block_entries_at_the_edges(lake) -> None:
    rules = SessionRules(entry_delay_minutes=15, entry_cutoff_minutes=15)
    bt, _, b, _ = run_intraday(lake, sessions=rules)
    assert b.fills
    for d in bt.decisions:
        minute = int((d.at.replace(tzinfo=None) - session_minutes(d.as_of.date())[0]) / ONE)
        if minute < 15:
            assert all(o.position_effect == "close" for o in d.orders)
    assert any(dr.reason == "opening" for d in bt.decisions for dr in d.dropped)


def test_flatten_at_close_ends_each_day_flat(lake) -> None:
    rules = SessionRules(flatten_at_close=True, flatten_minutes=5, entry_cutoff_minutes=10)
    bt, _, b, _ = run_intraday(lake, sessions=rules)
    flattened = [d for d in bt.decisions if d.flattened]
    assert flattened
    last_open = {d: session_minutes(d)[-1].replace(tzinfo=UTC) for d in DAYS}
    held_at_close: dict = {}
    position: dict[str, float] = {}
    for fill in sorted(b.fills, key=lambda f: f.filled_at):
        sign = 1 if fill.side == "buy" else -1
        position[fill.ticker] = position.get(fill.ticker, 0.0) + sign * fill.quantity
        day = _utc(fill.filled_at).date()
        if _utc(fill.filled_at) <= last_open[day]:
            held_at_close[day] = {t: q for t, q in position.items() if abs(q) > 1e-9}
    assert held_at_close[DAYS[0]] == {}


def test_config_rejects_daily_intervals() -> None:
    with pytest.raises(ValueError, match="intraday"):
        IntradayBacktestConfig(
            start=DAYS[0], end=DAYS[-1], universe=TICKERS, interval=Interval.DAY_1
        )


def test_needs_a_strategy(lake) -> None:
    config = IntradayBacktestConfig(start=DAYS[0], end=DAYS[-1], universe=TICKERS)
    with pytest.raises(ValueError, match="strateg"):
        IntradayBacktester([], broker(), lake, config)

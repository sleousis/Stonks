"""Intraday TCA (roadmap 21.3.5): spread from recorded quotes, arrival at the
next minute, shortfall per order and per sleeve, and the calibration
evidence. Everything runs on a synthetic recording."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

import pandas as pd
import pytest

from stonks.backtest.costs import CostModelSettings
from stonks.core.stream import QuoteTick, TradeTick
from stonks.production.intraday_tca import (
    MinuteBars,
    QuoteBook,
    calibrate_minute_costs,
    calibration_evidence,
    is_intraday_order,
    load_intraday_tca,
    summarize_intraday,
)
from stonks.store.state import SqliteState
from stonks.streaming.recorder import StreamRecorder

T = "ABC.US"
U = "XYZ.US"


def _ts(hh: int, mm: int, ss: float = 0.0) -> datetime:
    return datetime(2026, 9, 21, hh, mm, tzinfo=UTC) + timedelta(seconds=ss)


def _q(ticker: str, when: datetime, bid: float, ask: float) -> QuoteTick:
    return QuoteTick(ticker, when, bid=bid, ask=ask, bid_size=100, ask_size=100, source="test")


@pytest.fixture
def recording(tmp_path):
    root = tmp_path / "streams"
    with StreamRecorder(root) as rec:
        rec.write(_q(T, _ts(14, 30, 30), 99.98, 100.02))
        rec.write(TradeTick(T, _ts(14, 30, 40), 100.0, 10, "test"))
        rec.write(_q(T, _ts(14, 31, 0.2), 100.03, 100.07))
        rec.write(_q(U, _ts(14, 40, 0), 49.99, 50.01))
        rec.write(_q(T, _ts(14, 50, 0), 90.0, 110.0))  # after the calibration end
    return root


def _bars_frame(rows):
    return pd.DataFrame(rows, columns=["timestamp", "open", "close", "volume"])


BARS = {
    T: _bars_frame(
        [
            (datetime(2026, 9, 21, 14, 30), 99.9, 100.0, 4000),
            (datetime(2026, 9, 21, 14, 31), 100.05, 100.1, 5000),
            (datetime(2026, 9, 21, 14, 32), 100.1, 100.2, 3000),
            (datetime(2026, 9, 21, 14, 55), 101.0, 102.0, 3000),
        ]
    ),
    U: _bars_frame(
        [
            (datetime(2026, 9, 21, 14, 40), 50.0, 50.0, 2000),
            (datetime(2026, 9, 21, 14, 41), 49.95, 49.9, 2500),
        ]
    ),
}


def reader(ticker, start, end):
    frame = BARS.get(ticker, _bars_frame([]))
    mask = (frame["timestamp"] >= start.replace(tzinfo=None)) & (
        frame["timestamp"] <= end.replace(tzinfo=None)
    )
    return frame[mask]


# ---- quotes ------------------------------------------------------------------------


def test_quote_book_reads_the_last_quote_at_or_before(recording):
    book = QuoteBook.from_recording(recording)
    q = book.at(T, _ts(14, 31, 0))
    assert (q.bid, q.ask) == (99.98, 100.02)
    assert q.spread_bps == pytest.approx(4.0)
    assert q.half_spread_bps == pytest.approx(2.0)
    assert book.at(T, _ts(14, 31, 5)).bid == 100.03
    assert book.at(T, _ts(14, 30, 0)) is None  # nothing yet, never a later quote


def test_a_stale_quote_is_not_used(recording):
    book = QuoteBook.from_recording(recording)
    assert book.at(U, _ts(14, 45), max_age=timedelta(seconds=60)) is None
    assert book.at(U, _ts(14, 40, 30), max_age=timedelta(seconds=60)) is not None


def test_crossed_or_empty_quotes_are_skipped():
    book = QuoteBook([_q(T, _ts(14, 0), 10.0, 9.0), QuoteTick(T, _ts(14, 1), bid=None, ask=10.0)])
    assert book.at(T, _ts(14, 5)) is None
    assert book.count() == 0


def test_the_recording_end_bounds_the_book(recording):
    book = QuoteBook.from_recording(recording, end=_ts(14, 45))
    assert book.at(T, _ts(14, 55), max_age=timedelta(hours=1)).bid == 100.03


# ---- bars --------------------------------------------------------------------------


def test_arrival_is_the_next_minute_open():
    bars = MinuteBars(reader)
    assert bars.next_open(T, _ts(14, 31, 0)) == (_ts(14, 31), 100.05)
    assert bars.next_open(T, _ts(14, 31, 20)) == (_ts(14, 32), 100.1)
    assert bars.volume_at(T, _ts(14, 31, 30)) == 5000
    assert bars.session_close(T, _ts(14, 31)) == 102.0


def test_bars_after_the_end_are_not_seen():
    bars = MinuteBars(reader, end=_ts(14, 32, 30))
    assert bars.session_close(T, _ts(14, 31)) == 100.2
    assert bars.next_open(T, _ts(14, 33)) is None


# ---- orders ------------------------------------------------------------------------


@pytest.fixture
def state(tmp_path):
    st = SqliteState(tmp_path / "state.sqlite")
    st.migrate()
    yield st
    st.close()


def _order(state, cid, ticker, side, qty, decided, price, strategy, context=None, tick=None):
    state.execute(
        "INSERT OR IGNORE INTO strategies (id, class_path, params_json, status, created_at,"
        " updated_at) VALUES (?, 'x:Y', '{}', 'active', '2026-01-01', '2026-01-01')",
        [strategy],
    )
    ctx = {"trigger": "signal", "strategy_id": strategy, **(context or {"interval": "1m"})}
    state.execute(
        "INSERT INTO orders (client_id, tick_id, strategy_id, ticker, side, quantity, order_type,"
        " status, created_at, updated_at, portfolio_id, decision_price, decided_at,"
        " decision_context_json, expected_cost_bps)"
        " VALUES (?, ?, ?, ?, ?, ?, 'market', 'filled', ?, ?, 'pf_default', ?, ?, ?, 3.0)",
        [cid, tick, strategy, ticker, side, qty, decided.isoformat(), decided.isoformat(), price,
         decided.isoformat(), json.dumps(ctx)],
    )  # fmt: skip


def _fill(state, cid, ticker, qty, price, at, fee=0.0):
    state.execute(
        "INSERT INTO fills (order_client_id, ticker, quantity, price, fee, filled_at, portfolio_id)"
        " VALUES (?, ?, ?, ?, ?, ?, 'pf_default')",
        [cid, ticker, qty, price, fee, at.isoformat()],
    )


@pytest.fixture
def ledger(state):
    _order(state, "o1", T, "buy", 100, _ts(14, 31), 100.0, "orb")
    _fill(state, "o1", T, 100, 100.09, _ts(14, 31, 30), fee=0.5)
    _order(state, "o2", U, "sell", 50, _ts(14, 41), 50.0, "vwap")
    _fill(state, "o2", U, 50, 49.93, _ts(14, 41))
    _order(state, "d1", T, "buy", 10, _ts(20, 0), 100.0, "orb", context={"interval": "1d"})
    _order(state, "late", T, "buy", 10, _ts(14, 31), 100.0, "orb")
    _fill(state, "late", T, 10, 100.5, _ts(15, 30))
    return state


def test_intraday_orders_are_told_apart():
    assert is_intraday_order({"decision_context_json": '{"interval": "5m"}', "tick_id": "t"})
    assert not is_intraday_order({"decision_context_json": '{"interval": "1d"}', "tick_id": None})
    assert is_intraday_order({"decision_context_json": "{}", "tick_id": None, "origin": "strategy"})
    assert not is_intraday_order({"decision_context_json": "{}", "tick_id": "t1"})
    assert not is_intraday_order(
        {"decision_context_json": None, "tick_id": None, "origin": "manual"}
    )


def test_shortfall_with_next_minute_arrival_and_quoted_spread(ledger, recording):
    rows = load_intraday_tca(
        ledger, QuoteBook.from_recording(recording), MinuteBars(reader), "pf_default",
        until=_ts(14, 45),
    )  # fmt: skip
    by_id = {r.order.client_id: r for r in rows}
    assert set(by_id) == {"o1", "o2", "late"}  # the daily order is left out
    o1 = by_id["o1"]
    s = o1.order.shortfall
    assert s.arrival_price == pytest.approx(100.05)
    assert s.delay_bps == pytest.approx(5.0)
    assert s.impact_bps == pytest.approx(4.0)
    assert s.fee_bps == pytest.approx(0.5)
    assert s.convention_cost is None
    assert o1.arrival_source == "next_bar"
    assert o1.decision_spread_bps == pytest.approx(4.0)
    assert o1.fill_spread_bps == pytest.approx(0.04 / 100.05 * 1e4)
    assert o1.spread_cost_bps == pytest.approx(2.0)
    assert o1.residual_impact_bps == pytest.approx(2.0)
    [leg] = o1.legs
    assert leg.bar_volume == 5000
    # point in time: the fill after `until` is not seen, so the order reads unfilled
    assert by_id["late"].order.shortfall.filled_quantity == 0
    o2 = by_id["o2"].order.shortfall
    assert o2.delay_bps == pytest.approx(10.0)  # sold 49.95 against 50.00
    assert o2.impact_bps == pytest.approx(4.0)


def test_summary_per_strategy_sleeve(ledger, recording):
    rows = load_intraday_tca(
        ledger, QuoteBook.from_recording(recording), MinuteBars(reader), None,
        until=_ts(14, 45),
    )  # fmt: skip
    groups = {g.key: g for g in summarize_intraday(rows, "sleeve")}
    assert set(groups) == {"pf_default/orb", "pf_default/vwap"}
    orb = groups["pf_default/orb"]
    assert orb.base.orders == 2
    assert orb.base.delay_bps == pytest.approx(5.0)
    assert orb.decision_spread_bps == pytest.approx(4.0)
    assert orb.spread_cost_bps == pytest.approx(2.0)
    [total] = summarize_intraday(rows, "all")
    assert total.base.filled_orders == 2
    with pytest.raises(ValueError):
        summarize_intraday(rows, "colour")  # type: ignore[arg-type]


def test_calibration_evidence_is_point_in_time(ledger, recording):
    quotes = QuoteBook.from_recording(recording)
    rows = load_intraday_tca(ledger, quotes, MinuteBars(reader), None, until=_ts(14, 45))
    spreads, samples = calibration_evidence(
        rows, quotes, start=_ts(14, 0), end=_ts(14, 45), asset_classes={}
    )
    assert sorted(spreads["equity"]) == pytest.approx([0.02 / 100.05 * 1e4, 2.0, 2.0])
    assert len(samples) == 2
    first = next(s for s in samples if s.quantity == 100)
    assert first.cost_bps == pytest.approx(0.04 / 100.05 * 1e4)
    assert first.bar_volume == 5000


def test_calibrate_reads_only_data_up_to_the_end(ledger, recording):
    current = CostModelSettings.realistic()
    fit = calibrate_minute_costs(
        ledger, recording, reader, start=date(2026, 9, 21), end=date(2026, 9, 21),
        current=current, min_quotes=2, min_fills=2,
    )  # fmt: skip
    # the wide 14:50 quote and the 15:30 fill are on the end day, so they count
    assert fit.quotes["equity"] == 4
    assert fit.fills == 3
    early = calibrate_minute_costs(
        ledger, recording, reader, start=date(2026, 9, 20), end=date(2026, 9, 20),
        current=current,
    )  # fmt: skip
    assert early.fills == 0
    assert early.proposed == current

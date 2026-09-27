"""Session rules for the intraday engine (roadmap 21.2.4).

Hermetic: the exchange calendar comes from the local ``exchange_calendars``
data, and the fake calendar below needs nothing at all.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from pydantic import ValidationError

from stonks.core.types import Order
from stonks.engine.sessions import (
    HaltTable,
    SessionRulebook,
    SessionRules,
    TradingHalt,
    flatten_orders,
    gate_orders,
    session_state,
)
from stonks.scheduling.calendar import (
    AlwaysOpenCalendar,
    MarketCalendar,
    Session,
    get_calendar,
)

NYSE = get_calendar("XNYS")
RULES = SessionRules(entry_delay_minutes=5, entry_cutoff_minutes=10)
FLATTEN = SessionRules(
    entry_delay_minutes=5,
    entry_cutoff_minutes=10,
    flatten_at_close=True,
    flatten_minutes=5,
)


def utc(y: int, m: int, d: int, hh: int, mm: int = 0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=UTC)


class OneSession(MarketCalendar):
    """A single 10:00 to 16:00 UTC session on 2026-01-05."""

    name = "fake"

    def session(self, day: date) -> Session | None:
        if day != date(2026, 1, 5):
            return None
        return Session(day, utc(2026, 1, 5, 10), utc(2026, 1, 5, 16))


FAKE = OneSession()


def state(ts: datetime, ticker: str = "AAPL.US", **kw):  # type: ignore[no-untyped-def]
    kw.setdefault("calendar", FAKE)
    kw.setdefault("rules", RULES)
    return session_state(ts, ticker, **kw)


# ---- phases on a plain session ------------------------------------------------


def test_before_the_open_nothing_is_allowed() -> None:
    s = state(utc(2026, 1, 5, 9, 59))
    assert s.phase == "closed"
    assert not s.can_open and not s.can_close and not s.must_flatten
    assert s.session is None


def test_first_minutes_block_entries_but_allow_exits() -> None:
    s = state(utc(2026, 1, 5, 10, 0))
    assert s.phase == "opening"
    assert s.can_close and not s.can_open
    assert state(utc(2026, 1, 5, 10, 4)).phase == "opening"


def test_regular_hours_allow_everything() -> None:
    s = state(utc(2026, 1, 5, 10, 5))
    assert s.phase == "regular"
    assert s.can_open and s.can_close
    assert s.minutes_to_close == pytest.approx(355)


def test_last_minutes_block_entries() -> None:
    assert state(utc(2026, 1, 5, 15, 49)).phase == "regular"
    s = state(utc(2026, 1, 5, 15, 50))
    assert s.phase == "closing"
    assert s.can_close and not s.can_open and not s.must_flatten


def test_close_instant_is_closed() -> None:
    s = state(utc(2026, 1, 5, 16, 0))
    assert s.phase == "closed"
    assert not s.can_close


def test_flatten_window_only_when_the_book_asks() -> None:
    assert not state(utc(2026, 1, 5, 15, 56)).must_flatten
    s = state(utc(2026, 1, 5, 15, 55), rules=FLATTEN)
    assert s.phase == "flatten"
    assert s.must_flatten and s.can_close and not s.can_open
    assert state(utc(2026, 1, 5, 15, 54), rules=FLATTEN).phase == "closing"


def test_holiday_is_closed_all_day() -> None:
    assert state(utc(2026, 1, 6, 12)).phase == "closed"


def test_naive_timestamps_are_rejected() -> None:
    with pytest.raises(ValueError, match="naive"):
        state(datetime(2026, 1, 5, 12))


def test_zero_minutes_turn_the_edges_off() -> None:
    rules = SessionRules(entry_delay_minutes=0, entry_cutoff_minutes=0)
    assert state(utc(2026, 1, 5, 10, 0), rules=rules).can_open
    assert state(utc(2026, 1, 5, 15, 59), rules=rules).can_open


def test_rules_validate() -> None:
    with pytest.raises(ValidationError):
        SessionRules(entry_delay_minutes=-1)
    with pytest.raises(ValidationError):
        SessionRules(flatten_minutes=-5)
    with pytest.raises(ValidationError):
        SessionRules(flatten_at_close=True, flatten_minutes=0)


def test_rules_are_frozen() -> None:
    with pytest.raises(ValidationError):
        RULES.entry_delay_minutes = 3  # type: ignore[misc]


# ---- the real NYSE calendar: early closes, holidays and DST --------------------


def test_nyse_early_close_moves_every_rule() -> None:
    # 2026-11-27, the day after Thanksgiving: the close is 13:00 ET (18:00 UTC).
    assert state(utc(2026, 11, 27, 17, 49), calendar=NYSE).phase == "regular"
    assert state(utc(2026, 11, 27, 17, 50), calendar=NYSE).phase == "closing"
    assert state(utc(2026, 11, 27, 17, 55), calendar=NYSE, rules=FLATTEN).must_flatten
    after = state(utc(2026, 11, 27, 18, 30), calendar=NYSE)
    assert after.phase == "closed" and not after.can_close


def test_nyse_holiday_is_closed() -> None:
    # 2026-07-03, Independence Day observed on the Friday.
    assert state(utc(2026, 7, 3, 15), calendar=NYSE).phase == "closed"


def test_nyse_open_follows_dst() -> None:
    # US clocks moved forward on 2026-03-08. The open is 14:30 UTC before
    # and 13:30 UTC after.
    assert state(utc(2026, 3, 6, 14, 0), calendar=NYSE).phase == "closed"
    assert state(utc(2026, 3, 6, 14, 30), calendar=NYSE).phase == "opening"
    assert state(utc(2026, 3, 9, 13, 30), calendar=NYSE).phase == "opening"
    assert state(utc(2026, 3, 9, 14, 0), calendar=NYSE).phase == "regular"


def test_nyse_close_follows_dst_in_autumn() -> None:
    # US clocks moved back on 2026-11-01. The close is 20:00 UTC before and
    # 21:00 UTC after.
    assert state(utc(2026, 10, 30, 20, 30), calendar=NYSE).phase == "closed"
    assert state(utc(2026, 11, 2, 20, 30), calendar=NYSE).phase == "regular"
    assert state(utc(2026, 11, 2, 20, 52), calendar=NYSE).phase == "closing"
    assert state(utc(2026, 11, 2, 20, 55), calendar=NYSE, rules=FLATTEN).must_flatten


def test_session_date_is_the_exchange_local_date() -> None:
    s = state(utc(2026, 3, 9, 19), calendar=NYSE)
    assert s.session is not None and s.session.date == date(2026, 3, 9)


# ---- 24/7 calendars ------------------------------------------------------------


def test_always_open_calendar_has_no_edges() -> None:
    cal = AlwaysOpenCalendar()
    for ts in (utc(2026, 1, 5, 0, 0), utc(2026, 1, 5, 23, 59), utc(2026, 1, 3, 12)):
        s = state(ts, "BTC-USD.CC", calendar=cal, rules=FLATTEN)
        assert s.phase == "regular"
        assert s.can_open and not s.must_flatten


# ---- trading halts -------------------------------------------------------------


def test_halted_ticker_gets_no_orders() -> None:
    halts = HaltTable([TradingHalt("AAPL.US", utc(2026, 1, 5, 12), utc(2026, 1, 5, 12, 5), "luld")])
    s = state(utc(2026, 1, 5, 12, 2), halts=halts)
    assert s.halted and s.halt is not None and s.halt.reason == "luld"
    assert s.phase == "halted"
    assert not s.can_open and not s.can_close and not s.must_flatten
    other = state(utc(2026, 1, 5, 12, 2), "MSFT.US", halts=halts)
    assert other.can_open


def test_halt_ends_at_its_end_instant() -> None:
    halts = HaltTable([TradingHalt("AAPL.US", utc(2026, 1, 5, 12), utc(2026, 1, 5, 12, 5), "luld")])
    assert state(utc(2026, 1, 5, 11, 59), halts=halts).can_open
    assert state(utc(2026, 1, 5, 12, 5), halts=halts).can_open


def test_open_ended_halt_lasts_until_resumed() -> None:
    table = HaltTable([TradingHalt("AAPL.US", utc(2026, 1, 5, 11), None, "exchange")])
    assert state(utc(2026, 1, 5, 15), halts=table).halted
    resumed = table.resume("AAPL.US", utc(2026, 1, 5, 15, 30))
    assert state(utc(2026, 1, 5, 15), halts=resumed).halted
    assert not state(utc(2026, 1, 5, 15, 30), halts=resumed).halted
    # The table is a value: resuming returns a new one.
    assert state(utc(2026, 1, 5, 15, 30), halts=table).halted


def test_halt_during_flatten_window_blocks_the_flatten() -> None:
    halts = HaltTable([TradingHalt("AAPL.US", utc(2026, 1, 5, 15, 50), None, "luld")])
    s = state(utc(2026, 1, 5, 15, 57), rules=FLATTEN, halts=halts)
    assert s.halted and not s.must_flatten


def test_halt_validation() -> None:
    with pytest.raises(ValueError, match="naive"):
        TradingHalt("AAPL.US", datetime(2026, 1, 5, 12), None, "luld")
    with pytest.raises(ValueError, match="end"):
        TradingHalt("AAPL.US", utc(2026, 1, 5, 12), utc(2026, 1, 5, 11), "luld")
    with pytest.raises(ValueError, match="reason"):
        TradingHalt("AAPL.US", utc(2026, 1, 5, 12), None, "weather")  # type: ignore[arg-type]


def test_halt_table_add_and_active() -> None:
    table = HaltTable()
    assert table.active("AAPL.US", utc(2026, 1, 5, 12)) is None
    table = table.add(TradingHalt("AAPL.US", utc(2026, 1, 5, 12), None, "exchange"))
    assert table.active("AAPL.US", utc(2026, 1, 5, 12)) is not None
    assert len(table) == 1


def test_resume_without_an_open_halt_is_a_no_op() -> None:
    table = HaltTable()
    assert len(table.resume("AAPL.US", utc(2026, 1, 5, 12))) == 0


# ---- the rulebook: calendars by ticker -----------------------------------------


def test_rulebook_picks_the_calendar_by_ticker() -> None:
    book = SessionRulebook(RULES)
    # 2026-03-09 14:00 UTC: NYSE is 30 minutes in, crypto is always open.
    assert book.state(utc(2026, 3, 9, 14), "AAPL.US").phase == "regular"
    assert book.state(utc(2026, 3, 9, 12), "AAPL.US").phase == "closed"
    crypto = book.state(utc(2026, 3, 8, 12), "BTC-USD.CC", asset_class="crypto")
    assert crypto.can_open


def test_rulebook_unknown_calendar_is_closed() -> None:
    book = SessionRulebook(RULES)
    s = book.state(utc(2026, 3, 9, 14), "NOSUFFIX")
    assert s.phase == "closed" and not s.can_close


def test_rulebook_holds_halts() -> None:
    halts = HaltTable([TradingHalt("AAPL.US", utc(2026, 3, 9, 14), None, "luld")])
    book = SessionRulebook(RULES, halts=halts)
    assert book.state(utc(2026, 3, 9, 15), "AAPL.US").halted
    book = book.with_halts(HaltTable())
    assert not book.state(utc(2026, 3, 9, 15), "AAPL.US").halted


# ---- order gating ---------------------------------------------------------------


def order(ticker: str, side: str, qty: float, effect: str | None = None) -> Order:
    return Order(
        client_id=f"2026-01-05:s1:{ticker}:{side}",
        ticker=ticker,
        side=side,  # type: ignore[arg-type]
        quantity=qty,
        position_effect=effect,  # type: ignore[arg-type]
    )


def test_gate_keeps_everything_in_regular_hours() -> None:
    orders = [order("AAPL.US", "buy", 10), order("MSFT.US", "sell", 5)]
    kept, dropped = gate_orders(
        orders,
        {"MSFT.US": 5},
        lambda t: state(utc(2026, 1, 5, 12), t),
    )
    assert [o.ticker for o in kept] == ["AAPL.US", "MSFT.US"]
    assert dropped == []


def test_gate_drops_entries_but_keeps_exits_at_the_edges() -> None:
    orders = [order("AAPL.US", "buy", 10), order("MSFT.US", "sell", 5)]
    kept, dropped = gate_orders(orders, {"MSFT.US": 5}, lambda t: state(utc(2026, 1, 5, 15, 55), t))
    assert [(o.ticker, o.position_effect) for o in kept] == [("MSFT.US", "close")]
    assert [(d.order.ticker, d.reason) for d in dropped] == [("AAPL.US", "closing")]


def test_gate_splits_an_order_that_crosses_zero() -> None:
    # Long 5, sell 8 in the last minutes: the 5 that close go, the 3 that
    # would open a short do not.
    kept, dropped = gate_orders(
        [order("AAPL.US", "sell", 8)], {"AAPL.US": 5}, lambda t: state(utc(2026, 1, 5, 15, 55), t)
    )
    assert [(o.quantity, o.position_effect) for o in kept] == [(5, "close")]
    assert [(d.order.quantity, d.order.position_effect) for d in dropped] == [(3, "open")]


def test_gate_drops_every_order_for_a_halted_ticker() -> None:
    halts = HaltTable([TradingHalt("AAPL.US", utc(2026, 1, 5, 11), None, "luld")])
    kept, dropped = gate_orders(
        [order("AAPL.US", "sell", 5), order("MSFT.US", "buy", 1)],
        {"AAPL.US": 5},
        lambda t: state(utc(2026, 1, 5, 12), t, halts=halts),
    )
    assert [o.ticker for o in kept] == ["MSFT.US"]
    assert [(d.order.ticker, d.reason) for d in dropped] == [("AAPL.US", "halted")]


def test_gate_drops_everything_when_closed() -> None:
    kept, dropped = gate_orders(
        [order("AAPL.US", "sell", 5)], {"AAPL.US": 5}, lambda t: state(utc(2026, 1, 5, 17), t)
    )
    assert kept == [] and [d.reason for d in dropped] == ["closed"]


# ---- flatten orders -------------------------------------------------------------


def test_flatten_closes_every_position_in_the_window() -> None:
    at = utc(2026, 1, 5, 15, 56)
    orders = flatten_orders(
        {"AAPL.US": 10, "MSFT.US": -4, "FLAT.US": 0},
        lambda t: state(at, t, rules=FLATTEN),
        portfolio_id="pf_x",
    )
    got = {(o.ticker, o.side, o.quantity, o.position_effect, o.order_type) for o in orders}
    assert got == {
        ("AAPL.US", "sell", 10, "close", "market"),
        ("MSFT.US", "buy", 4, "close", "market"),
    }
    ids = {o.client_id for o in orders}
    assert ids == {
        "2026-01-05:pf_x:flatten:AAPL.US:sell",
        "2026-01-05:pf_x:flatten:MSFT.US:cover",
    }
    assert all(o.time_in_force == "day" and o.portfolio_id == "pf_x" for o in orders)


def test_flatten_uses_marketable_limits_when_prices_are_known() -> None:
    at = utc(2026, 1, 5, 15, 56)
    orders = flatten_orders(
        {"AAPL.US": 10, "MSFT.US": -4},
        lambda t: state(at, t, rules=FLATTEN),
        prices={"AAPL.US": 100.0, "MSFT.US": 50.0},
        collar_bps=20,
    )
    by = {o.ticker: o for o in orders}
    assert by["AAPL.US"].order_type == "limit"
    assert by["AAPL.US"].limit_price == pytest.approx(99.8)
    assert by["MSFT.US"].limit_price == pytest.approx(50.1)


def test_flatten_is_empty_outside_the_window_or_when_halted() -> None:
    early = flatten_orders(
        {"AAPL.US": 10}, lambda t: state(utc(2026, 1, 5, 15, 50), t, rules=FLATTEN)
    )
    assert early == []
    halts = HaltTable([TradingHalt("AAPL.US", utc(2026, 1, 5, 15), None, "luld")])
    halted = flatten_orders(
        {"AAPL.US": 10},
        lambda t: state(utc(2026, 1, 5, 15, 56), t, rules=FLATTEN, halts=halts),
    )
    assert halted == []


def test_state_reasons_explain_blocks() -> None:
    assert state(utc(2026, 1, 5, 10, 1)).reason == "opening"
    assert state(utc(2026, 1, 5, 12)).reason is None
    assert state(utc(2026, 1, 5, 17)).reason == "closed"


def test_minutes_to_close_is_none_when_closed() -> None:
    assert state(utc(2026, 1, 5, 17)).minutes_to_close is None
    s = state(utc(2026, 1, 5, 15, 30))
    assert s.minutes_to_close == pytest.approx(30)
    assert s.session is not None and s.session.close - s.at == timedelta(minutes=30)

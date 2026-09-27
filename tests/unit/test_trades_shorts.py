"""Phase 16.1: the trade ledger pairs short lots (sell-open, buy-close)."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pandas as pd
import pytest

from stonks.backtest.corporate_actions import CorporateActionRecord
from stonks.backtest.trades import build_round_trips
from stonks.core.types import Fill


def _ts(day: int) -> datetime:
    return datetime(2026, 3, day, tzinfo=UTC)


def _fill(side: str, qty: float, price: float, day: int, fee: float = 0.0, cid: str = "0:x"):
    return Fill(cid, "X", qty, price, fee, _ts(day), side)  # type: ignore[arg-type]


def test_short_then_cover_is_one_short_round_trip_by_hand() -> None:
    # Short 10 at 100 (fee 1), cover at 90 (fee 1): 10 x 10 - 2 = 98.
    [trip] = build_round_trips([_fill("sell", 10, 100.0, 2, 1.0), _fill("buy", 10, 90.0, 5, 1.0)])
    assert trip.side == "short" and not trip.is_open
    assert (trip.entry_px, trip.exit_px, trip.qty) == (100.0, 90.0, 10.0)
    assert trip.fees == pytest.approx(2.0)
    assert trip.pnl == pytest.approx(98.0)
    assert trip.return_pct == pytest.approx(98.0 / 1_001.0)


def test_a_losing_short() -> None:
    [trip] = build_round_trips([_fill("sell", 5, 50.0, 2), _fill("buy", 5, 60.0, 3)])
    assert trip.pnl == pytest.approx(-50.0)


def test_a_sell_that_crosses_zero_closes_the_long_and_opens_a_short() -> None:
    # Buy 10 at 10, sell 15 at 12 (fee 3): the long closes 10 (fee 2), the
    # short opens 5 (fee 1). Cover 5 at 11: short P&L 5 x 1 - 1 = 4.
    trips = build_round_trips(
        [_fill("buy", 10, 10.0, 2), _fill("sell", 15, 12.0, 3, 3.0), _fill("buy", 5, 11.0, 4)]
    )
    long_, short = trips
    assert (long_.side, long_.qty, long_.pnl) == ("long", 10.0, pytest.approx(20.0 - 2.0))
    assert (short.side, short.qty, short.pnl) == ("short", 5.0, pytest.approx(4.0))


def test_a_buy_that_crosses_zero_covers_then_opens_a_long() -> None:
    trips = build_round_trips([_fill("sell", 4, 20.0, 2), _fill("buy", 10, 15.0, 3)])
    closed, open_ = trips
    assert (closed.side, closed.pnl) == ("short", pytest.approx(20.0))
    assert (open_.side, open_.is_open, open_.qty) == ("long", True, 6.0)


def test_an_open_short_is_marked_at_the_last_mark() -> None:
    [trip] = build_round_trips([_fill("sell", 10, 100.0, 2)], marks={"X": 80.0})
    assert trip.is_open and trip.side == "short"
    assert trip.pnl == pytest.approx(200.0)


def test_a_split_and_a_dividend_on_a_short_lot() -> None:
    # Short 10 at 100; 2:1 split -> short 20 at 50; dividend 1.00 paid in
    # full on 20 shares = -20. Cover 20 at 45: 20 x 5 - 20 = 80.
    actions = [
        CorporateActionRecord(_ts(3), "X", "split", 2.0, -10.0, -20.0, 0.0),
        CorporateActionRecord(_ts(4), "X", "dividend", 1.0, -20.0, -20.0, -20.0),
    ]
    [trip] = build_round_trips(
        [_fill("sell", 10, 100.0, 2), _fill("buy", 20, 45.0, 5)], corporate_actions=actions
    )
    assert (trip.qty, trip.entry_px) == (20.0, 50.0)
    assert trip.dividends == pytest.approx(-20.0)
    assert trip.pnl == pytest.approx(80.0)


def test_short_excursions_are_mirrored() -> None:
    bars = pd.DataFrame(
        {
            "ticker": ["X"] * 3,
            "timestamp": [_ts(2), _ts(3), _ts(4)],
            "high": [100.0, 120.0, 95.0],
            "low": [98.0, 90.0, 80.0],
            "close": [100.0, 110.0, 85.0],
        }
    )
    [trip] = build_round_trips(
        [_fill("sell", 1, 100.0, 2), _fill("buy", 1, 85.0, 4)],
        bars=bars,
        timeline=[date(2026, 3, d) for d in (2, 3, 4)],
    )
    assert trip.mae_pct == pytest.approx(-0.20)  # the high of 120 hurt a short
    assert trip.mfe_pct == pytest.approx(0.20)  # the low of 80 helped it


def test_long_trips_are_labelled_long() -> None:
    [trip] = build_round_trips([_fill("buy", 1, 10.0, 2), _fill("sell", 1, 11.0, 3)])
    assert trip.side == "long"

"""Point-in-time back-adjustment of bar history for splits and dividends."""

from __future__ import annotations

from datetime import date, datetime

import numpy as np
import pandas as pd
import pytest

from stonks.core.corporate_actions import Dividend, Split
from stonks.features.price_adjustment import SeriesAdjustment


def _frame(closes, adj=None, start="2024-06-03") -> pd.DataFrame:
    days = pd.bdate_range(start, periods=len(closes))
    closes = np.asarray(closes, dtype=float)
    return pd.DataFrame(
        {
            "ticker": "X.US",
            "timestamp": [datetime(d.year, d.month, d.day) for d in days],
            "open": closes,
            "high": closes + 1,
            "low": closes - 1,
            "close": closes,
            "adj_close": closes if adj is None else np.asarray(adj, dtype=float),
            "volume": 1_000.0,
        }
    )


def test_no_events_and_adj_close_equal_to_close_is_identity():
    adj = SeriesAdjustment.build(_frame([10, 11, 12]), events=())
    assert adj.is_identity


def test_split_back_adjusts_prior_bars_and_volume():
    # 10:1 split effective on the 3rd bar (2024-06-05)
    frame = _frame([1000, 1010, 101, 102])
    adj = SeriesAdjustment.build(frame, events=(Split("X.US", date(2024, 6, 5), 10.0),))
    out = adj.apply(frame, lo=0, hi=4)
    assert out["close"].tolist() == pytest.approx([100, 101, 101, 102])
    assert out["open"].tolist() == pytest.approx([100, 101, 101, 102])
    assert out["high"].tolist() == pytest.approx([100.1, 101.1, 102, 103])
    assert out["volume"].tolist() == pytest.approx([10_000, 10_000, 1_000, 1_000])
    # raw input untouched
    assert frame["close"].tolist() == [1000, 1010, 101, 102]


def test_dividend_scales_prior_bars_by_one_minus_yield_on_prior_close():
    frame = _frame([100, 100, 98, 99])
    adj = SeriesAdjustment.build(frame, events=(Dividend("X.US", date(2024, 6, 5), 2.0),))
    out = adj.apply(frame, lo=0, hi=4)
    assert out["close"].tolist() == pytest.approx([98, 98, 98, 99])
    assert out["volume"].tolist() == [1_000.0] * 4  # dividends don't touch volume


def test_adjustment_is_causal_events_after_as_of_are_ignored():
    frame = _frame([1000, 1010, 101, 102])
    adj = SeriesAdjustment.build(frame, events=(Split("X.US", date(2024, 6, 5), 10.0),))
    # as of bar 1 (before the ex-date) the history is raw
    before = adj.apply(frame, lo=0, hi=2)
    assert before["close"].tolist() == [1000, 1010]
    assert adj.closes(frame["close"].to_numpy(), lo=0, hi=2).tolist() == [1000, 1010]


def test_latest_bar_as_of_is_always_raw():
    frame = _frame([100, 100, 98, 50, 51])
    events = (
        Dividend("X.US", date(2024, 6, 5), 2.0),
        Split("X.US", date(2024, 6, 6), 2.0),
    )
    adj = SeriesAdjustment.build(frame, events=events)
    for hi in range(1, 6):
        closes = adj.closes(frame["close"].to_numpy(), lo=0, hi=hi)
        assert closes[-1] == frame["close"].iloc[hi - 1]


def test_events_outside_the_series_have_no_effect():
    frame = _frame([10, 11, 12])
    events = (
        Split("X.US", date(2020, 1, 2), 4.0),  # before the first bar
        Split("X.US", date(2030, 1, 2), 4.0),  # after the last bar
    )
    adj = SeriesAdjustment.build(frame, events=events)
    assert adj.apply(frame, 0, 3)["close"].tolist() == pytest.approx([10, 11, 12])


def test_intraday_bars_adjust_from_first_bar_of_ex_date():
    ts = [datetime(2024, 6, 4, 15), datetime(2024, 6, 4, 16), datetime(2024, 6, 5, 14)]
    frame = pd.DataFrame(
        {
            "timestamp": ts,
            "open": [200.0, 200.0, 100.0],
            "high": [200.0, 200.0, 100.0],
            "low": [200.0, 200.0, 100.0],
            "close": [200.0, 200.0, 100.0],
            "adj_close": [200.0, 200.0, 100.0],
            "volume": [1.0, 1.0, 1.0],
        }
    )
    adj = SeriesAdjustment.build(frame, events=(Split("X.US", date(2024, 6, 5), 2.0),))
    assert adj.apply(frame, 0, 3)["close"].tolist() == pytest.approx([100, 100, 100])


def test_adj_close_fallback_when_no_events():
    # vendor adj_close encodes a 2:1 split effective on bar 2 (and nothing else)
    frame = _frame([200, 202, 101, 102], adj=[100, 101, 101, 102])
    adj = SeriesAdjustment.build(frame, events=())
    assert not adj.is_identity
    assert adj.apply(frame, 0, 4)["close"].tolist() == pytest.approx([100, 101, 101, 102])
    # causal: as of bar 1 the history is raw
    assert adj.apply(frame, 0, 2)["close"].tolist() == pytest.approx([200, 202])


def test_events_take_precedence_over_adj_close_no_double_adjustment():
    # adj_close already includes the split; events say the same thing
    frame = _frame([200, 202, 101, 102], adj=[100, 101, 101, 102])
    adj = SeriesAdjustment.build(frame, events=(Split("X.US", date(2024, 6, 5), 2.0),))
    assert adj.apply(frame, 0, 4)["close"].tolist() == pytest.approx([100, 101, 101, 102])


def test_adj_close_constant_ratio_is_identity():
    frame = _frame([10, 11, 12], adj=[5, 5.5, 6])
    assert SeriesAdjustment.build(frame, events=()).is_identity


def test_adj_close_missing_values_fall_back_to_neighbours():
    frame = _frame([200, 202, 101, 102], adj=[np.nan, 101, np.nan, 102])
    adj = SeriesAdjustment.build(frame, events=())
    assert adj.apply(frame, 0, 4)["close"].tolist() == pytest.approx([100, 101, 101, 102])


def test_empty_frame_is_identity():
    assert SeriesAdjustment.build(_frame([]), events=()).is_identity

"""Price adjustment properties (8.6, BL-49).

For any series and any splits and dividends: the latest bar as of a cutoff
reads its raw price, events after the cutoff never change the slice, and a
series whose raw prices only jump by its splits reads flat once adjusted.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pandas as pd
from hypothesis import given
from hypothesis import strategies as st

from stonks.core.corporate_actions import Dividend, Split
from stonks.features.price_adjustment import SeriesAdjustment

START = datetime(2024, 1, 1)


def _frame(closes: list[float]) -> pd.DataFrame:
    stamps = [START + timedelta(days=i) for i in range(len(closes))]
    return pd.DataFrame({"timestamp": stamps, "close": closes, "open": closes})


@st.composite
def series(draw):
    n = draw(st.integers(min_value=2, max_value=60))
    closes = draw(st.lists(st.floats(min_value=1.0, max_value=1_000.0), min_size=n, max_size=n))
    events = []
    for _ in range(draw(st.integers(min_value=0, max_value=4))):
        day = START + timedelta(days=draw(st.integers(min_value=0, max_value=n + 5)))
        if draw(st.booleans()):
            ratio = draw(st.sampled_from([0.5, 2.0, 3.0, 10.0, 0.1]))
            events.append(Split(ticker="A.US", ex_date=day.date(), ratio=ratio))
        else:
            amount = draw(st.floats(min_value=0.01, max_value=5.0))
            events.append(Dividend(ticker="A.US", ex_date=day.date(), amount=amount))
    events.sort(key=lambda e: e.ex_date)
    return closes, events


@given(series(), st.data())
def test_the_latest_bar_reads_raw(case, data):
    closes, events = case
    frame = _frame(closes)
    adj = SeriesAdjustment.build(frame, events)
    hi = data.draw(st.integers(min_value=1, max_value=len(closes)))
    lo = data.draw(st.integers(min_value=0, max_value=hi - 1))
    out = adj.closes(np.array(closes), lo, hi)
    assert out[-1] == closes[hi - 1]
    assert np.all(np.isfinite(out)) and np.all(out > 0)


@given(series(), st.data())
def test_events_after_the_cutoff_never_change_the_slice(case, data):
    closes, events = case
    frame = _frame(closes)
    hi = data.draw(st.integers(min_value=1, max_value=len(closes)))
    cutoff = frame["timestamp"].iloc[hi - 1]
    known = [e for e in events if pd.Timestamp(e.ex_date) <= cutoff]
    full = SeriesAdjustment.build(frame, events).closes(np.array(closes), 0, hi)
    seen = SeriesAdjustment.build(frame, known).closes(np.array(closes), 0, hi)
    np.testing.assert_allclose(full, seen, rtol=1e-12)


@given(
    st.floats(min_value=1.0, max_value=1_000.0),
    st.lists(
        st.tuples(st.integers(min_value=1, max_value=39), st.sampled_from([2.0, 3.0, 0.5, 4.0])),
        max_size=3,
        unique_by=lambda t: t[0],
    ),
)
def test_splits_alone_read_flat_once_adjusted(level, splits):
    n = 40
    factor = np.ones(n)
    events = []
    for idx, ratio in splits:
        factor[idx:] /= ratio  # raw prices drop by the ratio from the ex-date on
        events.append(
            Split(ticker="A.US", ex_date=(START + timedelta(days=idx)).date(), ratio=ratio)
        )
    closes = list(level * factor)
    adj = SeriesAdjustment.build(_frame(closes), sorted(events, key=lambda e: e.ex_date))
    out = adj.closes(np.array(closes), 0, n)
    np.testing.assert_allclose(out, closes[-1], rtol=1e-9)

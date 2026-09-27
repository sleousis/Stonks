"""Yahoo serves the VIX term-structure series as canonical macro rows (BL-46)."""

from __future__ import annotations

from datetime import date, datetime

import pandas as pd

from stonks.ingest.sources.yahoo import MACRO_SERIES
from tests.unit.test_yahoo_source import FakeYF, make_source


def _vix_frame():
    idx = pd.DatetimeIndex(
        [datetime(2026, 4, 1), datetime(2026, 4, 2), datetime(2026, 4, 3)], name="Date"
    ).tz_localize("America/Chicago")
    return pd.DataFrame(
        {
            "Open": [18.0, 19.0, 20.0],
            "High": [19.0, 21.0, 22.0],
            "Low": [17.0, 18.0, 19.0],
            "Close": [18.5, 20.5, None],
            "Adj Close": [18.5, 20.5, None],
            "Volume": [0, 0, 0],
        },
        index=idx,
    )


def test_vix_rows_use_canonical_names():
    fake = FakeYF(frames=[_vix_frame()])
    rows = list(make_source(fake).fetch_macro_indicator("usa", "vix_spot"))
    assert fake.calls[0][0] == "^VIX"
    assert [(r.observation_date, r.value) for r in rows] == [
        (date(2026, 4, 1), 18.5),
        (date(2026, 4, 2), 20.5),
    ]
    row = rows[0]
    assert (row.country_iso, row.indicator, row.period) == ("USA", "vix_spot", None)
    assert row.country_name == "United States"


def test_vix3m_maps_to_its_symbol():
    fake = FakeYF(frames=[_vix_frame()])
    rows = list(make_source(fake).fetch_macro_indicator("USA", "vix_3m"))
    assert fake.calls[0][0] == "^VIX3M" and rows[0].indicator == "vix_3m"
    assert MACRO_SERIES[("USA", "vix_3m")] == "^VIX3M"


def test_other_pairs_and_empty_frames_give_no_rows():
    fake = FakeYF(frames=[pd.DataFrame()])
    source = make_source(fake)
    assert list(source.fetch_macro_indicator("USA", "real_gdp_total")) == []
    assert fake.calls == []
    assert list(source.fetch_macro_indicator("USA", "vix_spot")) == []

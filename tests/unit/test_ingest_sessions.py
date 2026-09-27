"""When a daily bar is final (DS-02, DS-08). Hermetic: the market
calendars come from the installed ``exchange_calendars`` data."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pandas as pd

from stonks.ingest.sessions import (
    MarketSessionCloses,
    closed_sessions,
    drop_open_sessions,
    fallback_closes,
    is_final,
)

SESSIONS = MarketSessionCloses()


def test_nyse_holiday_has_no_session():
    # Friday 4 July 2025
    now = datetime(2025, 7, 10, tzinfo=UTC)
    assert closed_sessions(SESSIONS, "A.US", date(2025, 7, 4), date(2025, 7, 6), now) == []
    assert closed_sessions(SESSIONS, "A.US", date(2025, 7, 3), date(2025, 7, 7), now) == [
        date(2025, 7, 3),
        date(2025, 7, 7),
    ]


def test_session_is_final_only_after_the_close():
    day = [date(2025, 7, 1)]
    assert is_final(SESSIONS, "A.US", day, datetime(2025, 7, 1, 19, 59, tzinfo=UTC)) == [False]
    assert is_final(SESSIONS, "A.US", day, datetime(2025, 7, 1, 20, 0, tzinfo=UTC)) == [True]


def test_crypto_day_closes_at_utc_midnight():
    day = [date(2025, 7, 5)]  # a Saturday
    assert is_final(
        SESSIONS, "BTC-USD.CC", day, datetime(2025, 7, 5, 23, tzinfo=UTC), "crypto"
    ) == [False]
    assert is_final(SESSIONS, "BTC-USD.CC", day, datetime(2025, 7, 6, tzinfo=UTC), "crypto") == [
        True
    ]


def test_unknown_market_falls_back_to_weekdays():
    assert list(fallback_closes(date(2025, 7, 4), date(2025, 7, 7))) == [
        date(2025, 7, 4),
        date(2025, 7, 7),
    ]
    now = datetime(2025, 7, 8, tzinfo=UTC)
    assert closed_sessions(SESSIONS, "NOSUFFIX", date(2025, 7, 5), date(2025, 7, 6), now) == []


def test_drop_open_sessions_keeps_closed_rows():
    frame = pd.DataFrame({"date": [date(2025, 6, 30), date(2025, 7, 1)], "close": [1.0, 2.0]})
    out = drop_open_sessions(frame, SESSIONS, "A.US", datetime(2025, 7, 1, 15, tzinfo=UTC))
    assert out["date"].tolist() == [date(2025, 6, 30)]


def test_the_session_cache_is_bounded():
    """BE-62: a long-running server asks for many ranges; the cache keeps
    only the most recent ones."""
    from datetime import timedelta

    from stonks.ingest.sessions import MarketSessionCloses

    sessions = MarketSessionCloses(max_entries=8)
    start = date(2024, 1, 1)
    for i in range(30):
        sessions.closes("A.US", start, start + timedelta(days=i))
    assert len(sessions._cache) <= 8
    # a recent range is still served from the cache
    key_count = len(sessions._cache)
    sessions.closes("A.US", start, start + timedelta(days=29))
    assert len(sessions._cache) == key_count

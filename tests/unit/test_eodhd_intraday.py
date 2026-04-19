"""Unit tests for the EODHD intraday parser + fetch method."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from stonks.core.interval import Interval
from stonks.ingest.schemas import IntradayBar
from stonks.ingest.sources.eodhd import (
    EodhdDataSource,
    EodhdFreeTierError,
    parse_intraday_response,
)


def test_parse_intraday_response_converts_epoch_to_utc_datetime():
    payload = [
        {"timestamp": 1776087000, "gmtoffset": 0, "datetime": "2026-04-13 13:30:00",
         "open": 259.85, "high": 260.18, "low": 258.00, "close": 258.60, "volume": 1_600_710},
        {"timestamp": 1776087300, "gmtoffset": 0, "datetime": "2026-04-13 13:35:00",
         "open": 258.60, "high": 258.90, "low": 258.10, "close": 258.75, "volume": 800_000},
    ]
    bars = list(parse_intraday_response("AAPL.US", payload))
    assert len(bars) == 2
    first = bars[0]
    assert isinstance(first, IntradayBar)
    assert first.ticker == "AAPL.US"
    assert first.timestamp == datetime.fromtimestamp(1776087000, tz=UTC)
    assert first.close == 258.60
    assert first.volume == 1_600_710


def test_parse_intraday_skips_rows_without_timestamp_or_close():
    payload = [
        {"open": 100, "high": 101, "low": 99, "close": 100, "volume": 10},   # no timestamp
        {"timestamp": 1776087000, "open": 100, "high": 101, "low": 99, "volume": 10},  # no close
        {"timestamp": 1776087300, "open": 100, "high": 101, "low": 99, "close": 100.5, "volume": 10},
    ]
    bars = list(parse_intraday_response("X.US", payload))
    assert len(bars) == 1
    assert bars[0].close == 100.5


def test_parse_intraday_allows_null_volume_post_market():
    payload = [{"timestamp": 1776456000, "open": 270.23, "high": 270.23,
                "low": 270.23, "close": 270.23, "volume": None}]
    bars = list(parse_intraday_response("AAPL.US", payload))
    assert len(bars) == 1
    assert bars[0].volume is None


def test_parse_intraday_empty_for_non_list_payload():
    assert list(parse_intraday_response("X.US", {})) == []
    assert list(parse_intraday_response("X.US", None)) == []


def test_parse_intraday_raises_on_free_tier_string():
    with pytest.raises(EodhdFreeTierError):
        list(parse_intraday_response("X.US", "Only EOD data allowed for free users."))


# ---- EodhdDataSource.fetch_intraday_bars -----------------------------------


def test_fetch_intraday_rejects_non_native_interval():
    source = EodhdDataSource(api_key="k")
    with pytest.raises(ValueError, match="intraday"):
        list(source.fetch_intraday_bars("AAPL.US", Interval.HOUR_4))


def test_fetch_intraday_accepts_native_intervals():
    """The validation gate passes for 1m, 5m, 1h; we're not asserting
    network behavior here — only that the internal dispatch allows each
    native interval through without raising."""
    source = EodhdDataSource(api_key="k")
    for iv in (Interval.MIN_1, Interval.MIN_5, Interval.HOUR_1):
        assert iv.code in source._INTRADAY_NATIVE  # type: ignore[attr-defined]


def test_fetch_intraday_builds_expected_query_params():
    """Uses a fake session to capture the GET params and verify that the
    interval and the date→epoch conversion are wired correctly."""
    import json

    class _FakeResp:
        status_code = 200
        text = "[]"

        def raise_for_status(self): pass
        def json(self): return []

    class _FakeSession:
        def __init__(self):
            self.calls = []

        def get(self, url, params=None, timeout=None):
            self.calls.append((url, params))
            return _FakeResp()

    session = _FakeSession()
    source = EodhdDataSource(api_key="k", session=session)  # type: ignore[arg-type]
    list(source.fetch_intraday_bars(
        "AAPL.US", Interval.MIN_5,
        since=date(2026, 4, 1), until=date(2026, 4, 2),
    ))
    url, params = session.calls[0]
    assert url.endswith("/intraday/AAPL.US")
    assert params["interval"] == "5m"
    # since = 2026-04-01 00:00:00 UTC
    assert params["from"] == str(int(
        datetime(2026, 4, 1, tzinfo=UTC).timestamp()
    ))
    # until = 2026-04-02 23:59:59 UTC (end-of-day)
    assert params["to"] == str(int(
        datetime(2026, 4, 2, 23, 59, 59, tzinfo=UTC).timestamp()
    ))
    _ = json  # silence unused; kept for readability of fake structure

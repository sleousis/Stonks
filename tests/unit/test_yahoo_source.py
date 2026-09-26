"""YahooDataSource against a fake ``yfinance.Ticker`` (hermetic, no network)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from stonks.core.interval import Interval
from stonks.ingest.sources.base import DataSourceError
from stonks.ingest.sources.yahoo import (
    YahooDataSource,
    YahooDataSourceError,
    YahooUnsupportedOperationError,
    YahooUnsupportedTickerError,
)

NOW = datetime(2026, 9, 25, 20, 0, tzinfo=UTC)


class FakeTicker:
    def __init__(self, symbol, frames, info, calls, splits=None, split_calls=None):
        self.symbol = symbol
        self._frames = frames
        self._info = info
        self._calls = calls
        self._splits = splits
        self._split_calls = split_calls if split_calls is not None else []

    def get_splits(self, period="max"):
        self._split_calls.append(self.symbol)
        if self._splits is None:
            return pd.Series(dtype=float)
        return self._splits

    def history(self, **kwargs):
        self._calls.append((self.symbol, kwargs))
        frame = self._frames.pop(0) if self._frames else pd.DataFrame()
        if isinstance(frame, Exception):
            raise frame
        return frame

    @property
    def info(self):
        self._calls.append((self.symbol, "info"))
        if isinstance(self._info, Exception):
            raise self._info
        return self._info


class FakeYF:
    def __init__(self, frames=None, info=None, splits=None):
        self.frames = list(frames or [])
        self.info = info if info is not None else {}
        self.splits = splits
        self.calls: list = []
        self.split_calls: list = []

    def __call__(self, symbol):
        return FakeTicker(symbol, self.frames, self.info, self.calls, self.splits, self.split_calls)


def make_source(fake, **kwargs):
    kwargs.setdefault("min_request_interval_seconds", 0.0)
    kwargs.setdefault("retry_backoff_seconds", 0.0)
    return YahooDataSource(ticker_factory=fake, now=lambda: NOW, sleep=lambda s: None, **kwargs)


def daily_frame(tz="America/New_York"):
    idx = pd.DatetimeIndex(
        [datetime(2026, 4, 1), datetime(2026, 4, 2), datetime(2026, 4, 3)], name="Date"
    ).tz_localize(tz)
    return pd.DataFrame(
        {
            "Open": [100.0, 101.0, np.nan],
            "High": [105.0, 106.0, np.nan],
            "Low": [99.0, 100.0, np.nan],
            "Close": [104.0, 102.0, np.nan],
            "Adj Close": [103.0, 101.5, np.nan],
            "Volume": [1_000_000, 900_000, 0],
        },
        index=idx,
    )


# ---- fetch_prices -----------------------------------------------------------


def test_fetch_prices_maps_ohlcv_and_adj_close():
    fake = FakeYF(frames=[daily_frame()])
    bars = list(make_source(fake).fetch_prices("AAPL.US", since=date(2026, 4, 1)))

    assert [b.date for b in bars] == [date(2026, 4, 1), date(2026, 4, 2)]  # NaN row dropped
    first = bars[0]
    assert first.ticker == "AAPL.US"
    assert (first.open, first.high, first.low, first.close) == (100.0, 105.0, 99.0, 104.0)
    assert first.adj_close == 103.0
    assert first.volume == 1_000_000
    assert isinstance(first.volume, int)


def test_fetch_prices_requests_unadjusted_ohlc_with_inclusive_until():
    fake = FakeYF(frames=[daily_frame()])
    make_source(fake).fetch_prices("BMW.XETRA", since=date(2026, 4, 1), until=date(2026, 4, 3))

    symbol, kwargs = fake.calls[0]
    assert symbol == "BMW.DE"
    assert kwargs["interval"] == "1d"
    assert kwargs["auto_adjust"] is False  # keep raw OHLC + separate Adj Close
    assert kwargs["start"] == "2026-04-01"
    assert kwargs["end"] == "2026-04-04"  # yfinance's end is exclusive


def test_fetch_prices_without_since_asks_for_full_history():
    fake = FakeYF(frames=[daily_frame()])
    make_source(fake).fetch_prices("AAPL.US")
    _, kwargs = fake.calls[0]
    assert kwargs["period"] == "max"
    assert "start" not in kwargs


def test_fetch_prices_until_only_still_fetches_full_history():
    # yfinance silently narrows an end-only request to one month unless
    # period="max" is passed alongside it.
    fake = FakeYF(frames=[daily_frame()])
    make_source(fake).fetch_prices("AAPL.US", until=date(2026, 4, 3))
    _, kwargs = fake.calls[0]
    assert kwargs["period"] == "max"
    assert kwargs["end"] == "2026-04-04"
    assert "start" not in kwargs


def test_fetch_prices_uses_exchange_local_date_not_utc_date():
    # Tokyo-style exchange: local midnight is the previous day in UTC. The
    # trading date must stay the exchange-local date.
    fake = FakeYF(frames=[daily_frame(tz="Asia/Hong_Kong")])
    bars = list(make_source(fake).fetch_prices("0700.HK", since=date(2026, 4, 1)))
    assert bars[0].date == date(2026, 4, 1)


def test_fetch_prices_empty_frame_returns_no_rows():
    fake = FakeYF(frames=[pd.DataFrame()])
    assert list(make_source(fake).fetch_prices("AAPL.US")) == []


def test_fetch_prices_fills_missing_adj_close_and_volume():
    frame = daily_frame().drop(columns=["Adj Close"])
    frame["Volume"] = [np.nan, 5.0, 0]
    fake = FakeYF(frames=[frame])
    bars = list(make_source(fake).fetch_prices("AAPL.US"))
    assert bars[0].adj_close == bars[0].close
    assert bars[0].volume is None
    assert bars[1].volume == 5


def test_fetch_prices_flattens_multiindex_columns():
    frame = daily_frame()
    frame.columns = pd.MultiIndex.from_product([frame.columns, ["AAPL"]], names=["Price", "Ticker"])
    fake = FakeYF(frames=[frame])
    bars = list(make_source(fake).fetch_prices("AAPL.US"))
    assert len(bars) == 2
    assert bars[0].adj_close == 103.0


def test_fetch_prices_crypto_symbol():
    fake = FakeYF(frames=[daily_frame(tz="UTC")])
    bars = list(make_source(fake).fetch_prices("BTC-USD.CC"))
    assert fake.calls[0][0] == "BTC-USD"
    assert bars[0].ticker == "BTC-USD.CC"


def _splits(when, ratio, tz="America/New_York"):
    idx = pd.DatetimeIndex([when], name="Date").tz_localize(tz)
    return pd.Series([ratio], index=idx, name="Stock Splits")


def test_fetch_prices_undoes_yahoo_split_adjustment_on_ohlcv():
    # Yahoo's OHLCV is split-adjusted even with auto_adjust=False; the lake's
    # close is the as-traded price (EODHD semantics), adj_close the adjusted.
    fake = FakeYF(frames=[daily_frame()], splits=_splits(datetime(2026, 4, 2), 4.0))
    bars = list(make_source(fake).fetch_prices("AAPL.US", since=date(2026, 4, 1)))

    before, on = bars
    assert (before.open, before.high, before.low, before.close) == (400.0, 420.0, 396.0, 416.0)
    assert before.volume == 250_000
    assert before.adj_close == 103.0  # adjusted series unchanged
    assert (on.close, on.volume, on.adj_close) == (102.0, 900_000, 101.5)


def test_splits_after_the_window_still_unadjust():
    fake = FakeYF(frames=[daily_frame()], splits=_splits(datetime(2027, 1, 5), 2.0))
    bars = list(
        make_source(fake).fetch_prices("AAPL.US", since=date(2026, 4, 1), until=date(2026, 4, 3))
    )
    assert bars[0].close == 208.0


def test_intraday_bars_undo_split_adjustment():
    fake = FakeYF(frames=[intraday_frame()], splits=_splits(datetime(2026, 9, 25), 2.0))
    bars = list(
        make_source(fake).fetch_intraday_bars("AAPL.US", Interval.MIN_5, since=date(2026, 9, 24))
    )
    assert bars[0].close == 22.0
    assert bars[0].adj_close == 11.0
    assert bars[0].volume == 50


def test_crypto_skips_split_lookup():
    fake = FakeYF(frames=[daily_frame(tz="UTC")])
    make_source(fake).fetch_prices("BTC-USD.CC")
    assert fake.split_calls == []


def test_empty_history_skips_split_lookup():
    fake = FakeYF(frames=[pd.DataFrame()])
    make_source(fake).fetch_prices("AAPL.US")
    assert fake.split_calls == []


def test_http_4xx_is_not_retried():
    from curl_cffi.requests.exceptions import HTTPError

    class _Resp:
        status_code = 404

    fake = FakeYF(frames=[HTTPError("HTTP Error 404", response=_Resp())] * 3)
    with pytest.raises(YahooDataSourceError, match="404"):
        make_source(fake).fetch_prices("ZZZZ.US")
    assert len(fake.calls) == 1


def test_http_5xx_and_connection_errors_are_retried():
    from curl_cffi.requests.exceptions import ConnectionError as CurlConnectionError
    from curl_cffi.requests.exceptions import HTTPError

    class _Resp:
        status_code = 503

    fake = FakeYF(
        frames=[
            HTTPError("HTTP Error 503", response=_Resp()),
            CurlConnectionError("reset"),
            daily_frame(),
        ]
    )
    assert len(list(make_source(fake).fetch_prices("AAPL.US"))) == 2
    assert len(fake.calls) == 3


def test_unmapped_ticker_soft_fails_without_calling_yahoo():
    fake = FakeYF()
    with pytest.raises(YahooUnsupportedTickerError):
        make_source(fake).fetch_prices("GC.COMM")
    assert fake.calls == []


# ---- errors + rate limiting -------------------------------------------------


def test_yfinance_exception_becomes_data_source_error():
    from yfinance.exceptions import YFPricesMissingError

    fake = FakeYF(frames=[YFPricesMissingError("ZZZZ", "")])
    with pytest.raises(YahooDataSourceError) as info:
        make_source(fake).fetch_prices("ZZZZ.US")
    assert isinstance(info.value, DataSourceError)


def test_rate_limit_is_retried_with_backoff():
    from yfinance.exceptions import YFRateLimitError

    sleeps: list[float] = []
    fake = FakeYF(frames=[YFRateLimitError(), daily_frame()])
    source = YahooDataSource(
        ticker_factory=fake,
        now=lambda: NOW,
        sleep=sleeps.append,
        max_retries=3,
        retry_backoff_seconds=2.0,
        min_request_interval_seconds=0.0,
    )
    bars = list(source.fetch_prices("AAPL.US"))
    assert len(bars) == 2
    assert sleeps == [2.0]


def test_rate_limit_exhausted_raises_data_source_error():
    from yfinance.exceptions import YFRateLimitError

    fake = FakeYF(frames=[YFRateLimitError()] * 3)
    with pytest.raises(YahooDataSourceError, match="rate"):
        make_source(fake, max_retries=2).fetch_prices("AAPL.US")
    assert len(fake.calls) == 3


def test_min_request_interval_throttles_consecutive_calls():
    sleeps: list[float] = []
    clock = iter([0.0, 0.1, 0.1, 0.1])
    fake = FakeYF(frames=[daily_frame(), daily_frame()])
    source = YahooDataSource(
        ticker_factory=fake,
        now=lambda: NOW,
        sleep=sleeps.append,
        monotonic=lambda: next(clock),
        min_request_interval_seconds=0.5,
    )
    source.fetch_prices("BTC-USD.CC")
    source.fetch_prices("ETH-USD.CC")
    assert sleeps == [pytest.approx(0.4)]


def test_yfinance_global_config_raises_instead_of_hiding_errors():
    import yfinance as yf

    make_source(FakeYF())
    assert yf.config.debug.hide_exceptions is False


# ---- fetch_intraday_bars ----------------------------------------------------


def intraday_frame():
    idx = pd.DatetimeIndex(
        [datetime(2026, 9, 24, 9, 30), datetime(2026, 9, 24, 9, 35)], name="Datetime"
    ).tz_localize("America/New_York")
    return pd.DataFrame(
        {
            "Open": [10.0, 11.0],
            "High": [12.0, 12.5],
            "Low": [9.5, 10.5],
            "Close": [11.0, np.nan],
            "Adj Close": [11.0, np.nan],
            "Volume": [100, 0],
        },
        index=idx,
    )


def test_intraday_timestamps_are_naive_utc():
    fake = FakeYF(frames=[intraday_frame()])
    bars = list(
        make_source(fake).fetch_intraday_bars("AAPL.US", Interval.MIN_5, since=date(2026, 9, 24))
    )
    assert len(bars) == 1  # NaN close dropped
    assert bars[0].timestamp == datetime(2026, 9, 24, 13, 30)  # 09:30 EDT == 13:30 UTC
    assert bars[0].timestamp.tzinfo is None
    assert bars[0].adj_close == 11.0


@pytest.mark.parametrize(
    ("interval", "yahoo"),
    [
        (Interval.MIN_1, "1m"),
        (Interval.MIN_5, "5m"),
        (Interval.MIN_15, "15m"),
        (Interval.MIN_30, "30m"),
        (Interval.HOUR_1, "1h"),
    ],
)
def test_intraday_interval_mapping(interval, yahoo):
    fake = FakeYF(frames=[intraday_frame()])
    make_source(fake).fetch_intraday_bars("AAPL.US", interval, since=date(2026, 9, 24))
    assert fake.calls[0][1]["interval"] == yahoo


def test_intraday_unsupported_interval_raises_value_error():
    with pytest.raises(ValueError, match="4h"):
        make_source(FakeYF()).fetch_intraday_bars("AAPL.US", Interval.HOUR_4)


def test_intraday_since_beyond_lookback_raises_clear_error():
    fake = FakeYF()
    with pytest.raises(YahooDataSourceError, match="60 days"):
        make_source(fake).fetch_intraday_bars(
            "AAPL.US", Interval.MIN_5, since=NOW.date() - timedelta(days=90)
        )
    assert fake.calls == []


def test_intraday_default_since_is_the_lookback_window():
    fake = FakeYF(frames=[intraday_frame()])
    make_source(fake).fetch_intraday_bars("AAPL.US", Interval.HOUR_1)
    _, kwargs = fake.calls[0]
    start = date.fromisoformat(kwargs["start"])
    assert (NOW.date() - start).days < 730
    assert (NOW.date() - start).days > 700


def test_intraday_earliest_start_leaves_a_timezone_margin():
    # Yahoo measures its window in wall time from "now"; a start at local
    # midnight in an east-of-UTC exchange is up to ~a day further back than
    # the UTC date suggests, so the earliest accepted date keeps a margin.
    source = make_source(FakeYF(frames=[intraday_frame()] * 5))
    with pytest.raises(YahooDataSourceError, match="30 days"):
        source.fetch_intraday_bars("0700.HK", Interval.MIN_1, since=NOW.date() - timedelta(days=29))
    fake = FakeYF(frames=[intraday_frame()] * 5)
    make_source(fake).fetch_intraday_bars("0700.HK", Interval.MIN_1)
    assert fake.calls[0][1]["start"] == (NOW.date() - timedelta(days=28)).isoformat()


def test_intraday_1m_is_chunked_into_windows_yahoo_accepts():
    fake = FakeYF(frames=[intraday_frame(), pd.DataFrame(), pd.DataFrame()])
    make_source(fake).fetch_intraday_bars(
        "AAPL.US", Interval.MIN_1, since=date(2026, 9, 5), until=date(2026, 9, 25)
    )
    windows = [(k["start"], k["end"]) for _, k in fake.calls]
    assert windows == [
        ("2026-09-05", "2026-09-12"),
        ("2026-09-12", "2026-09-19"),
        ("2026-09-19", "2026-09-26"),
    ]


def test_intraday_chunks_deduplicate_boundary_bars():
    fake = FakeYF(frames=[intraday_frame(), intraday_frame()])
    bars = list(
        make_source(fake).fetch_intraday_bars(
            "AAPL.US", Interval.MIN_1, since=date(2026, 9, 12), until=date(2026, 9, 25)
        )
    )
    assert len(bars) == 1


# ---- metadata / fundamentals / discovery -------------------------------------


def test_fetch_metadata_maps_profile_fields():
    info = {
        "quoteType": "EQUITY",
        "longName": "Apple Inc.",
        "shortName": "Apple",
        "sector": "Technology",
        "industry": "Consumer Electronics",
        "currency": "USD",
        "exchange": "NMS",
        "logo_url": "https://example.test/logo.png",
    }
    fake = FakeYF(info=info)
    bundle = make_source(fake).fetch_metadata("AAPL.US")
    profile = bundle.profile
    assert profile.id == "AAPL.US"
    assert profile.asset_class == "equity"
    assert profile.name == "Apple Inc."
    assert profile.sector == "Technology"
    assert profile.industry == "Consumer Electronics"
    assert profile.currency == "USD"
    # Canonical exchange code, not Yahoo's "NMS".
    assert profile.exchange == "US"
    assert profile.security_type is None  # Yahoo's EQUITY is ambiguous


@pytest.mark.parametrize(("quote_type", "expected"), [("ETF", "etf"), ("MUTUALFUND", "fund")])
def test_fetch_metadata_normalizes_fund_security_types(quote_type, expected):
    fake = FakeYF(info={"quoteType": quote_type, "currency": "USD"})
    assert make_source(fake).fetch_metadata("SPY.US").profile.security_type == expected


def test_fetch_metadata_crypto_profile():
    fake = FakeYF(info={"quoteType": "CRYPTOCURRENCY", "currency": "USD", "name": "Bitcoin USD"})
    profile = make_source(fake).fetch_metadata("BTC-USD.CC").profile
    assert profile.asset_class == "crypto"
    assert profile.exchange == "CC"
    assert profile.currency == "USD"
    assert profile.sector is None


def test_fetch_metadata_empty_info_raises_soft_fail():
    fake = FakeYF(info={"trailingPegRatio": None})
    with pytest.raises(YahooDataSourceError):
        make_source(fake).fetch_metadata("ZZZZ.US")


def test_fetch_fundamentals_is_unsupported():
    with pytest.raises(YahooUnsupportedOperationError):
        make_source(FakeYF()).fetch_fundamentals("AAPL.US")


def test_list_tickers_is_unsupported():
    with pytest.raises(YahooUnsupportedOperationError):
        make_source(FakeYF()).list_tickers("US")


def test_list_exchanges_reports_mapped_codes():
    codes = {e.code for e in make_source(FakeYF()).list_exchanges()}
    assert {"US", "XETRA", "LSE", "CC"} <= codes
    assert "COMM" not in codes


def test_source_id():
    assert make_source(FakeYF()).source_id == "yahoo"

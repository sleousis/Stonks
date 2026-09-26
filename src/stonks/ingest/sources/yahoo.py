"""Yahoo Finance data source, wrapping the ``yfinance`` library.

``yfinance`` types (DataFrames with vendor column names, ``YFException``,
Yahoo symbols) stay inside this module: callers see canonical tickers
(``AAPL.US``), canonical row schemas, and :class:`DataSourceError`.

Ticker mapping
--------------
Canonical tickers use the EODHD-style ``<SYMBOL>.<EXCHANGE>`` form. Yahoo
uses a bare symbol for US listings, a vendor suffix for other exchanges
(``BMW.DE``), and a bare pair for crypto (``BTC-USD``). The closed map
:data:`_EXCHANGE_TO_YAHOO_SUFFIX` is the single source of truth; exchanges
missing from it raise :class:`YahooUnsupportedTickerError`. Commodity and
bond tickers are deliberately unmapped: Yahoo's futures (``GC=F``) and
yield indices (``^TNX``) are different instruments from the canonical
spot/benchmark series, so silently substituting them would corrupt bars.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd

from stonks.core.interval import Interval
from stonks.core.types import AssetClass
from stonks.ingest.metadata_bundle import MetadataBundle
from stonks.ingest.schemas import (
    ExchangeInfo,
    FinancialStatementsBundle,
    IntradayBar,
    RawPriceBar,
    TickerProfile,
)
from stonks.ingest.sources.base import DataSource, DataSourceError
from stonks.ingest.sources.eodhd import classify_asset_class
from stonks.logging import get_logger


class YahooDataSourceError(DataSourceError):
    """Base for Yahoo adapter errors (soft-fail per ticker)."""


class YahooUnsupportedTickerError(YahooDataSourceError):
    """The canonical ticker (or Yahoo symbol) has no mapping."""


class YahooUnsupportedOperationError(YahooDataSourceError):
    """Yahoo does not offer reliable data for this operation."""


class YahooLookbackError(YahooDataSourceError):
    """The requested window starts before Yahoo's intraday lookback limit."""


class YahooRateLimitError(YahooDataSourceError):
    """Yahoo kept rate-limiting after all retries."""


# Canonical exchange code -> Yahoo symbol suffix ("" = no suffix, US).
_EXCHANGE_TO_YAHOO_SUFFIX: dict[str, str] = {
    "US": "",
    "LSE": ".L",
    "XETRA": ".DE",
    "F": ".F",
    "PA": ".PA",
    "AS": ".AS",
    "BR": ".BR",
    "LS": ".LS",
    "MC": ".MC",
    "MI": ".MI",
    "SW": ".SW",
    "VI": ".VI",
    "CO": ".CO",
    "ST": ".ST",
    "OL": ".OL",
    "HE": ".HE",
    "IR": ".IR",
    "TO": ".TO",
    "V": ".V",
    "AU": ".AX",
    "HK": ".HK",
    "SHG": ".SS",
    "SHE": ".SZ",
    "KO": ".KS",
    "KQ": ".KQ",
    "NSE": ".NS",
    "BSE": ".BO",
    "SA": ".SA",
    "MX": ".MX",
    "JSE": ".JO",
    "TA": ".TA",
}
_YAHOO_SUFFIX_TO_EXCHANGE: dict[str, str] = {
    suffix: code for code, suffix in _EXCHANGE_TO_YAHOO_SUFFIX.items() if suffix
}
_CRYPTO_EXCHANGE = "CC"


def _split(ticker: str) -> tuple[str, str]:
    if "." not in ticker:
        raise YahooUnsupportedTickerError(
            f"{ticker!r} has no exchange suffix; expected canonical form like AAPL.US"
        )
    symbol, exchange = ticker.rsplit(".", 1)
    if not symbol:
        raise YahooUnsupportedTickerError(f"{ticker!r} has an empty symbol")
    return symbol, exchange.upper()


def to_yahoo_symbol(ticker: str) -> str:
    """Map a canonical ticker (``BMW.XETRA``) to its Yahoo symbol (``BMW.DE``)."""
    symbol, exchange = _split(ticker)
    if exchange == _CRYPTO_EXCHANGE:
        return symbol
    suffix = _EXCHANGE_TO_YAHOO_SUFFIX.get(exchange)
    if suffix is None:
        raise YahooUnsupportedTickerError(
            f"{ticker!r}: exchange {exchange!r} has no Yahoo Finance mapping "
            f"(supported: {sorted([*_EXCHANGE_TO_YAHOO_SUFFIX, _CRYPTO_EXCHANGE])})"
        )
    return f"{symbol}{suffix}"


def from_yahoo_symbol(symbol: str, asset_class: AssetClass = "equity") -> str:
    """Map a Yahoo symbol back to the canonical ticker.

    Yahoo's bare symbols are ambiguous (``BTC-USD`` vs a US stock), so the
    caller states the asset class; only ``equity`` and ``crypto`` map.
    """
    if not symbol or any(ch in symbol for ch in "=^"):
        raise YahooUnsupportedTickerError(f"Yahoo symbol {symbol!r} has no canonical mapping")
    if asset_class == "crypto":
        if "." in symbol:
            raise YahooUnsupportedTickerError(
                f"Yahoo crypto symbol {symbol!r} should be a bare pair like BTC-USD"
            )
        return f"{symbol}.{_CRYPTO_EXCHANGE}"
    if asset_class != "equity":
        raise YahooUnsupportedTickerError(
            f"Yahoo symbol {symbol!r}: asset class {asset_class!r} is not supported"
        )
    if "." not in symbol:
        return f"{symbol}.US"
    base, suffix = symbol.rsplit(".", 1)
    exchange = _YAHOO_SUFFIX_TO_EXCHANGE.get(f".{suffix.upper()}")
    if exchange is None or not base:
        raise YahooUnsupportedTickerError(
            f"Yahoo symbol {symbol!r}: suffix .{suffix} has no canonical exchange"
        )
    return f"{base}.{exchange}"


# ---- intraday limits ----------------------------------------------------------


@dataclass(frozen=True)
class _IntradaySpec:
    yahoo_interval: str
    lookback_days: int  # how far back from today Yahoo serves this interval
    max_window_days: int | None = None  # max span per request (None = no cap)


# Canonical interval code -> Yahoo interval + Yahoo's documented limits.
# Coarser sub-daily bars (4h/6h/12h) come from ``stonks ingest aggregate``.
_INTRADAY_SPECS: dict[str, _IntradaySpec] = {
    "1m": _IntradaySpec("1m", lookback_days=30, max_window_days=7),
    "5m": _IntradaySpec("5m", lookback_days=60),
    "15m": _IntradaySpec("15m", lookback_days=60),
    "30m": _IntradaySpec("30m", lookback_days=60),
    "1h": _IntradaySpec("1h", lookback_days=730),
}

# Yahoo ``quoteType`` -> canonical ``security_type``. ``EQUITY`` is left
# unmapped on purpose: it covers common, preferred and ADR listings alike.
_SECURITY_TYPE_MAP: dict[str, str] = {"ETF": "etf", "MUTUALFUND": "fund"}

_OHLC = ("Open", "High", "Low", "Close")


# ---- frame helpers --------------------------------------------------------------


def _flatten_columns(frame: pd.DataFrame) -> pd.DataFrame:
    """``yf.download`` (and some ``history`` paths) return (field, ticker)
    MultiIndex columns; keep only the level that holds the OHLCV names."""
    if not isinstance(frame.columns, pd.MultiIndex):
        return frame
    for level in range(frame.columns.nlevels):
        if "Close" in set(frame.columns.get_level_values(level)):
            out = frame.copy()
            out.columns = frame.columns.get_level_values(level)
            return out
    raise YahooDataSourceError(f"unexpected yfinance columns: {list(frame.columns)}")


def _clean_ohlcv(frame: pd.DataFrame | None) -> pd.DataFrame:
    """Drop rows without a close, fill missing O/H/L and adj close from the
    close. Volume NaN handling happens per row in :func:`_volume`."""
    if frame is None or frame.empty:
        return pd.DataFrame()
    frame = _flatten_columns(frame)
    if "Close" not in frame.columns:
        raise YahooDataSourceError(f"yfinance frame lacks a Close column: {list(frame.columns)}")
    frame = frame[frame["Close"].notna()].copy()
    for col in (*_OHLC, "Adj Close"):
        if col in frame.columns:
            frame[col] = frame[col].fillna(frame["Close"])
        else:
            frame[col] = frame["Close"]
    if "Volume" not in frame.columns:
        frame["Volume"] = None
    return frame


def _volume(value: Any) -> int | None:
    if value is None:
        return None
    f = float(value)
    return None if math.isnan(f) else round(f)


def _to_naive_utc(index: pd.DatetimeIndex) -> pd.DatetimeIndex:
    if index.tz is None:
        return index
    return index.tz_convert("UTC").tz_localize(None)


def _to_local_dates(index: pd.DatetimeIndex) -> list[date]:
    # Daily bars are stamped at exchange-local midnight; the local calendar
    # date is the trading date (converting to UTC first would shift Asian
    # sessions onto the previous day).
    return [ts.date() for ts in _local_dates(index)]


def _local_dates(index: pd.DatetimeIndex) -> pd.DatetimeIndex:
    """Exchange-local calendar dates (tz dropped, time floored to midnight)."""
    if index.tz is not None:
        index = index.tz_localize(None)
    return index.normalize()


def _unsplit(frame: pd.DataFrame, splits: pd.Series | None) -> pd.DataFrame:
    """Undo Yahoo's split adjustment of OHLCV.

    Even with ``auto_adjust=False`` Yahoo back-adjusts OHLC (and volume) for
    splits; only ``Adj Close`` additionally reflects dividends. The lake's
    ``close`` is the as-traded price (``adj_close`` carries the adjusted
    series), so multiply prices, and divide volume, by the product of every
    split ratio whose ex-date falls after the bar's local date.
    """
    if splits is None or len(splits) == 0 or frame.empty:
        return frame
    split_days = _local_dates(pd.DatetimeIndex(splits.index))
    ratios = pd.to_numeric(pd.Series(splits.to_numpy()), errors="coerce").to_numpy(dtype=float)
    bar_days = _local_dates(pd.DatetimeIndex(frame.index))
    factor = np.ones(len(frame))
    for day, ratio in zip(split_days, ratios, strict=True):
        if not math.isfinite(ratio) or ratio <= 0:
            continue
        factor = np.where(bar_days < day, factor * ratio, factor)
    if np.all(factor == 1.0):
        return frame
    out = frame.copy()
    for col in _OHLC:
        out[col] = out[col] * factor
    out["Volume"] = pd.to_numeric(out["Volume"], errors="coerce") / factor
    return out


def _iter_rows(frame: pd.DataFrame) -> Iterator[tuple[Any, dict[str, Any]]]:
    cols = [*_OHLC, "Adj Close", "Volume"]
    for idx, values in zip(frame.index, frame[cols].itertuples(index=False), strict=True):
        yield idx, dict(zip(cols, values, strict=True))


# ---- data source ---------------------------------------------------------------


class YahooDataSource(DataSource):
    """Prices, intraday bars and basic profiles from Yahoo Finance.

    Supported: :meth:`fetch_prices`, :meth:`fetch_intraday_bars`,
    :meth:`fetch_metadata` (profile only), :meth:`list_exchanges`.
    Unsupported (raise :class:`YahooUnsupportedOperationError`, which the
    pipeline soft-fails): :meth:`fetch_fundamentals`, :meth:`list_tickers`.
    Macro indicators inherit the empty default.
    """

    source_id = "yahoo"

    def __init__(
        self,
        *,
        timeout_seconds: int = 30,
        max_retries: int = 3,
        retry_backoff_seconds: float = 2.0,
        min_request_interval_seconds: float = 0.5,
        ticker_factory: Callable[[str], Any] | None = None,
        now: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        import yfinance as yf

        # By default yfinance logs errors and returns an empty frame, which
        # would file an invalid or delisted symbol as "ok, 0 rows". Make it
        # raise so failures map onto DataSourceError.
        yf.config.debug.hide_exceptions = False
        self._ticker_factory = ticker_factory or yf.Ticker
        self._timeout = timeout_seconds
        self._max_retries = max_retries
        self._backoff = retry_backoff_seconds
        self._min_interval = min_request_interval_seconds
        self._now = now or (lambda: datetime.now(UTC))
        self._sleep = sleep
        self._monotonic = monotonic
        self._last_request: float | None = None
        self._log = get_logger("stonks.ingest.yahoo")

    # ---- discovery ----

    def list_tickers(self, exchange: str) -> list[str]:
        raise YahooUnsupportedOperationError(
            "Yahoo Finance has no ticker-listing endpoint; pass --tickers explicitly"
        )

    def list_exchanges(self) -> list[ExchangeInfo]:
        codes = sorted([*_EXCHANGE_TO_YAHOO_SUFFIX, _CRYPTO_EXCHANGE])
        return [ExchangeInfo(code=code) for code in codes]

    # ---- bars ----

    def fetch_prices(
        self, ticker: str, since: date | None = None, until: date | None = None
    ) -> Iterable[RawPriceBar]:
        symbol = to_yahoo_symbol(ticker)
        kwargs: dict[str, Any] = {"interval": "1d"}
        if since is None:
            # Without period="max" yfinance narrows an end-only request to 1mo.
            kwargs["period"] = "max"
        else:
            kwargs["start"] = since.isoformat()
        if until is not None:
            kwargs["end"] = (until + timedelta(days=1)).isoformat()  # end is exclusive
        frame = _clean_ohlcv(self._history(symbol, **kwargs))
        if frame.empty:
            return []
        frame = _unsplit(frame, self._splits(ticker, symbol))
        days = _to_local_dates(pd.DatetimeIndex(frame.index))
        return [
            RawPriceBar(
                ticker=ticker,
                date=day,
                open=float(row["Open"]),
                high=float(row["High"]),
                low=float(row["Low"]),
                close=float(row["Close"]),
                adj_close=float(row["Adj Close"]),
                volume=_volume(row["Volume"]),
            )
            for day, (_, row) in zip(days, _iter_rows(frame), strict=True)
        ]

    def fetch_intraday_bars(
        self,
        ticker: str,
        interval: Interval,
        since: date | None = None,
        until: date | None = None,
    ) -> Iterable[IntradayBar]:
        spec = _INTRADAY_SPECS.get(interval.code)
        if spec is None:
            raise ValueError(
                f"Yahoo intraday supports {sorted(_INTRADAY_SPECS)}; "
                f"use aggregation for {interval.code!r}"
            )
        symbol = to_yahoo_symbol(ticker)
        today = self._now().astimezone(UTC).date()
        earliest = today - timedelta(days=spec.lookback_days - 1)
        if since is None:
            since = earliest
        elif since < earliest:
            raise YahooLookbackError(
                f"{ticker}: Yahoo serves {interval.code} bars only for the last "
                f"{spec.lookback_days} days (earliest {earliest.isoformat()}), "
                f"got since={since.isoformat()}"
            )
        end = (until or today) + timedelta(days=1)  # end is exclusive

        bars: dict[datetime, IntradayBar] = {}
        splits: pd.Series | None = None
        for start, stop in _windows(since, end, spec.max_window_days):
            frame = _clean_ohlcv(
                self._history(
                    symbol,
                    interval=spec.yahoo_interval,
                    start=start.isoformat(),
                    end=stop.isoformat(),
                )
            )
            if frame.empty:
                continue
            if splits is None:
                splits = self._splits(ticker, symbol)
            frame = _unsplit(frame, splits)
            stamps = _to_naive_utc(pd.DatetimeIndex(frame.index))
            for ts, (_, row) in zip(stamps, _iter_rows(frame), strict=True):
                when = ts.to_pydatetime()
                bars[when] = IntradayBar(
                    ticker=ticker,
                    timestamp=when,
                    open=float(row["Open"]),
                    high=float(row["High"]),
                    low=float(row["Low"]),
                    close=float(row["Close"]),
                    adj_close=float(row["Adj Close"]),
                    volume=_volume(row["Volume"]),
                )
        return [bars[k] for k in sorted(bars)]

    # ---- fundamentals / metadata ----

    def fetch_fundamentals(self, ticker: str) -> FinancialStatementsBundle:
        raise YahooUnsupportedOperationError(
            f"{ticker}: financial statements are not ingested from Yahoo Finance "
            "(no filing dates, unstable line items); use the eodhd source"
        )

    def fetch_metadata(self, ticker: str) -> MetadataBundle:
        symbol = to_yahoo_symbol(ticker)
        info = self._call(symbol, lambda t: t.info)
        if not isinstance(info, dict) or not info.get("quoteType"):
            raise YahooDataSourceError(f"{ticker}: Yahoo returned no profile for {symbol!r}")
        asset_class = classify_asset_class(ticker)
        is_equity = asset_class == "equity"
        profile = TickerProfile(
            id=ticker,
            asset_class=asset_class,
            # Canonical exchange code from our ticker, not Yahoo's ("NMS").
            exchange=ticker.rsplit(".", 1)[1].upper(),
            currency=_str_or_none(info.get("currency")),
            name=_str_or_none(info.get("longName") or info.get("shortName") or info.get("name")),
            sector=_str_or_none(info.get("sector")) if is_equity else None,
            industry=_str_or_none(info.get("industry")) if is_equity else None,
            security_type=_SECURITY_TYPE_MAP.get(str(info["quoteType"]).upper()),
        )
        return MetadataBundle(profile=profile)

    # ---- transport ----

    def _splits(self, ticker: str, symbol: str) -> pd.Series:
        """Full split history (one extra request per ticker). Crypto has no
        splits, so it is skipped."""
        if classify_asset_class(ticker) != "equity":
            return pd.Series(dtype=float)
        splits = self._call(symbol, lambda t: t.get_splits(period="max"))
        return splits if isinstance(splits, pd.Series) else pd.Series(dtype=float)

    def _history(self, symbol: str, **kwargs: Any) -> pd.DataFrame:
        # auto_adjust=False keeps raw OHLC plus a separate "Adj Close", which
        # is exactly our (close, adj_close) pair.
        kwargs.setdefault("auto_adjust", False)
        kwargs.setdefault("actions", False)
        kwargs.setdefault("timeout", self._timeout)
        return self._call(symbol, lambda t: t.history(**kwargs))

    def _call(self, symbol: str, fn: Callable[[Any], Any]) -> Any:
        from curl_cffi.requests.exceptions import RequestException as CurlRequestException
        from yfinance.exceptions import YFException, YFRateLimitError

        attempt = 0
        while True:
            self._throttle()
            try:
                return fn(self._ticker_factory(symbol))
            except (YFRateLimitError, CurlRequestException) as exc:
                rate_limited = isinstance(exc, YFRateLimitError) or _http_status(exc) == 429
                status = _http_status(exc)
                if not rate_limited and status is not None and status < 500:
                    # 4xx (unknown symbol, bad request): retrying won't help.
                    raise YahooDataSourceError(f"{symbol}: HTTP {status}: {exc}") from exc
                if attempt >= self._max_retries:
                    error_cls = YahooRateLimitError if rate_limited else YahooDataSourceError
                    kind = "rate limited" if rate_limited else "transport error"
                    raise error_cls(
                        f"{symbol}: {kind} after {attempt + 1} attempts: {exc}"
                    ) from exc
                delay = self._backoff * (2**attempt)
                self._log.warning(
                    "yahoo.retry", symbol=symbol, attempt=attempt + 1, delay=delay, error=str(exc)
                )
                self._sleep(delay)
                attempt += 1
            except YFException as exc:
                raise YahooDataSourceError(f"{symbol}: {exc}") from exc

    def _throttle(self) -> None:
        if self._min_interval <= 0:
            return
        if self._last_request is not None:
            elapsed = self._monotonic() - self._last_request
            if elapsed < self._min_interval:
                self._sleep(self._min_interval - elapsed)
        self._last_request = self._monotonic()


def _http_status(exc: BaseException) -> int | None:
    status = getattr(getattr(exc, "response", None), "status_code", None)
    return status if isinstance(status, int) else None


def _windows(start: date, end: date, max_days: int | None) -> Iterator[tuple[date, date]]:
    """Split ``[start, end)`` into consecutive windows of at most ``max_days``."""
    if max_days is None:
        yield start, end
        return
    cursor = start
    while cursor < end:
        stop = min(cursor + timedelta(days=max_days), end)
        yield cursor, stop
        cursor = stop


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None

"""EODHD data source.

Two parts, cleanly separable:
  - pure response parsers (``parse_prices_response`` / ``parse_fundamentals_response``)
    — unit-testable with captured fixtures, no HTTP.
  - :class:`EodhdDataSource` — the HTTP client that calls the API and hands the
    JSON to those parsers.

Free-tier responses for restricted endpoints come back as a plain-text error
message; we detect them and raise :class:`EodhdFreeTierError` so the pipeline
can record the failure cleanly.

Why not the official ``eodhd`` PyPI SDK? Evaluated; it is a thin wrapper that
(a) silently swallows all HTTP errors and returns empty ``{}`` — there is no
way for us to distinguish a free-tier 403 from a genuinely empty response,
breaking our ``EodhdFreeTierError`` domain signal; (b) has no retry/backoff
and no concurrency — our ``fetch_metadata`` runs five endpoints in parallel,
roughly 5× faster; (c) pulls in ~100 MB of transitive deps (matplotlib,
pillow, websockets) that we don't need. This custom client is ~270 LOC,
injectable-session-friendly for tests, and has full coverage.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from typing import Any

import requests

from stonks.core.interval import Interval
from stonks.ingest.metadata_bundle import MetadataBundle
from stonks.ingest.schemas import (
    AnalystEstimateRow,
    AnalystRatingsRow,
    DividendRow,
    EmployeeCountRow,
    FundamentalRow,
    InsiderTransactionRow,
    IntradayBar,
    MarketCapRow,
    NewsArticleRow,
    NewsSentimentRow,
    RawPriceBar,
    SharesOutstandingRow,
    StockSplitRow,
    TickerProfile,
)
from stonks.ingest.sources.base import DataSource
from stonks.logging import get_logger

_FREE_TIER_TEXT = "only eod data allowed"
_FREE_TIER_WARNING_KEY = "warning"

_STATEMENT_MAP = {
    "Income_Statement": "income",
    "Balance_Sheet": "balance",
    "Cash_Flow": "cashflow",
}
_FREQUENCY_MAP = {"quarterly": "Q", "yearly": "A"}


class EodhdFreeTierError(RuntimeError):
    """Raised when the API signals an endpoint is blocked on the free tier."""


# ---- pure parsers (unit-tested) --------------------------------------------


def parse_prices_response(ticker: str, payload: Any) -> Iterator[RawPriceBar]:
    _check_free_tier(payload)
    if not isinstance(payload, list):
        return iter(())
    for row in payload:
        if not isinstance(row, dict):
            continue
        if _FREE_TIER_WARNING_KEY in row and "date" not in row:
            raise EodhdFreeTierError(str(row.get(_FREE_TIER_WARNING_KEY)))
        if "date" not in row:
            continue
        yield RawPriceBar(
            ticker=ticker,
            date=row["date"],
            open=row["open"],
            high=row["high"],
            low=row["low"],
            close=row["close"],
            adj_close=row.get("adjusted_close", row["close"]),
            volume=row.get("volume"),
        )


def parse_intraday_response(ticker: str, payload: Any) -> Iterator[IntradayBar]:
    """Parse ``/api/intraday/{ticker}`` into IntradayBar entries.

    Each row has a Unix ``timestamp`` (seconds since epoch); we convert to
    a UTC ``datetime``. Volume can legitimately be ``None`` for post-market
    bars on thin names.
    """
    _check_free_tier(payload)
    if not isinstance(payload, list):
        return iter(())
    for row in payload:
        if not isinstance(row, dict):
            continue
        ts = row.get("timestamp")
        if ts is None:
            continue
        try:
            ts_int = int(ts)
        except (TypeError, ValueError):
            continue
        when = datetime.fromtimestamp(ts_int, tz=UTC)
        close_val = row.get("close")
        if close_val is None:
            continue
        yield IntradayBar(
            ticker=ticker,
            timestamp=when,
            open=row.get("open", close_val),
            high=row.get("high", close_val),
            low=row.get("low", close_val),
            close=close_val,
            adj_close=close_val,
            volume=row.get("volume"),
        )


def parse_fundamentals_response(ticker: str, payload: Any) -> Iterator[FundamentalRow]:
    _check_free_tier(payload)
    if not isinstance(payload, dict):
        return iter(())
    return _iter_fundamentals(ticker, payload)


def _iter_fundamentals(ticker: str, payload: dict) -> Iterator[FundamentalRow]:
    financials = payload.get("Financials") or {}
    for vendor_statement, canonical in _STATEMENT_MAP.items():
        section = financials.get(vendor_statement) or {}
        for vendor_freq, canonical_freq in _FREQUENCY_MAP.items():
            periods = section.get(vendor_freq) or {}
            for period_key, period_dict in periods.items():
                if not isinstance(period_dict, dict):
                    continue
                period_end = _parse_date(period_dict.get("date") or period_key)
                if period_end is None:
                    continue
                for line_item, raw_value in period_dict.items():
                    if line_item == "date":
                        continue
                    yield FundamentalRow(
                        ticker=ticker,
                        period_end=period_end,
                        frequency=canonical_freq,
                        statement=canonical,
                        line_item=line_item,
                        value=_coerce_optional_float(raw_value),
                    )


def _check_free_tier(payload: Any) -> None:
    if isinstance(payload, str) and _FREE_TIER_TEXT in payload.lower():
        raise EodhdFreeTierError(payload.strip())


def _parse_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    return None


def _coerce_optional_float(value: Any) -> float | None:
    if value is None or value == "None" or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_optional_int(value: Any) -> int | None:
    v = _coerce_optional_float(value)
    return int(v) if v is not None else None


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        # EODHD's news feed sometimes returns "YYYY-MM-DD HH:MM:SS" with no
        # offset. We assume UTC in that case so downstream code never has to
        # juggle naive timestamps.
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    return None


# ---- extended-fundamentals parsers (pure, unit-tested) ---------------------


def parse_profile_from_fundamentals(ticker: str, payload: Any) -> TickerProfile | None:
    """Extract the TickerProfile from the fundamentals JSON blob."""
    if not isinstance(payload, dict):
        return None
    general = payload.get("General") or {}
    shares_stats = payload.get("SharesStats") or {}
    technicals = payload.get("Technicals") or {}

    return TickerProfile(
        id=ticker,
        exchange=general.get("Exchange"),
        currency=general.get("CurrencyCode"),
        name=general.get("Name"),
        country_iso=general.get("CountryISO"),
        ipo_date=_parse_date(general.get("IPODate")),
        sector=general.get("Sector"),
        industry=general.get("Industry"),
        fiscal_year_end=general.get("FiscalYearEnd"),
        web_url=general.get("WebURL"),
        is_delisted=bool(general.get("IsDelisted", False)),
        is_bank=bool(general.get("IsBank", False)),
        beta=_coerce_optional_float(technicals.get("Beta")),
        short_percent=_coerce_optional_float(shares_stats.get("ShortPercent")),
        insider_ownership_percent=_coerce_optional_float(shares_stats.get("PercentInsiders")),
        institutional_ownership_percent=_coerce_optional_float(
            shares_stats.get("PercentInstitutions")
        ),
        employee_count=_coerce_optional_int(general.get("FullTimeEmployees")),
        esg_score=_coerce_optional_float((payload.get("ESGScores") or {}).get("TotalESG")),
    )


def parse_analyst_estimates_from_fundamentals(
    ticker: str, payload: Any
) -> Iterator[AnalystEstimateRow]:
    """Flatten Earnings.History into one row per (period, metric)."""
    if not isinstance(payload, dict):
        return iter(())
    history = ((payload.get("Earnings") or {}).get("History")) or {}
    return _iter_analyst_estimates(ticker, history)


def _iter_analyst_estimates(ticker: str, history: dict) -> Iterator[AnalystEstimateRow]:
    _metric_keys = ("epsActual", "epsEstimate", "epsDifference", "surprisePercent")
    for period_key, row in history.items():
        if not isinstance(row, dict):
            continue
        period_end = _parse_date(row.get("date") or period_key)
        if period_end is None:
            continue
        for metric in _metric_keys:
            if metric in row:
                yield AnalystEstimateRow(
                    ticker=ticker,
                    period_end=period_end,
                    metric=metric,
                    value=_coerce_optional_float(row[metric]),
                )


def parse_analyst_ratings_from_fundamentals(
    ticker: str, payload: Any
) -> AnalystRatingsRow | None:
    if not isinstance(payload, dict):
        return None
    ratings = payload.get("AnalystRatings")
    if not isinstance(ratings, dict):
        return None
    return AnalystRatingsRow(
        ticker=ticker,
        rating=_coerce_optional_float(ratings.get("Rating")),
        target_price=_coerce_optional_float(ratings.get("TargetPrice")),
        strong_buy=_coerce_optional_int(ratings.get("StrongBuy")) or 0,
        buy=_coerce_optional_int(ratings.get("Buy")) or 0,
        hold=_coerce_optional_int(ratings.get("Hold")) or 0,
        sell=_coerce_optional_int(ratings.get("Sell")) or 0,
        strong_sell=_coerce_optional_int(ratings.get("StrongSell")) or 0,
    )


def parse_shares_outstanding_history(
    ticker: str, payload: Any
) -> Iterator[SharesOutstandingRow]:
    """Fundamentals has an ``outstandingShares`` section with both
    ``annual`` and ``quarterly`` dicts — each period dict carries
    ``dateFormatted`` (ISO date) and ``shares`` (integer count). We pull
    the full history from both, merged. Duplicate ``(ticker, date)`` rows
    (same period appearing in annual + quarterly) are idempotent in the
    lake via ON CONFLICT, so we emit them all and let the DB dedupe.
    """
    if not isinstance(payload, dict):
        return iter(())
    shares_blob = payload.get("outstandingShares")
    if not isinstance(shares_blob, dict):
        return iter(())
    return _iter_shares_outstanding(ticker, shares_blob)


def _iter_shares_outstanding(
    ticker: str, shares_blob: dict
) -> Iterator[SharesOutstandingRow]:
    for frequency_key in ("annual", "quarterly"):
        periods = shares_blob.get(frequency_key) or {}
        if not isinstance(periods, dict):
            continue
        for period_entry in periods.values():
            if not isinstance(period_entry, dict):
                continue
            d = _parse_date(period_entry.get("dateFormatted") or period_entry.get("date"))
            shares = _coerce_optional_float(period_entry.get("shares"))
            if d is None or shares is None or shares < 0:
                continue
            yield SharesOutstandingRow(ticker=ticker, date=d, shares=shares)


def parse_employee_count_snapshot(
    ticker: str, payload: Any, as_of: date | None = None
) -> EmployeeCountRow | None:
    if not isinstance(payload, dict):
        return None
    count = _coerce_optional_int((payload.get("General") or {}).get("FullTimeEmployees"))
    if count is None:
        return None
    return EmployeeCountRow(ticker=ticker, date=as_of or date.today(), count=count)


def parse_dividends_response(ticker: str, payload: Any) -> Iterator[DividendRow]:
    """Parse /api/div/<ticker> into DividendRow entries."""
    _check_free_tier(payload)
    if not isinstance(payload, list):
        return iter(())
    for row in payload:
        if not isinstance(row, dict):
            continue
        ex_date = _parse_date(row.get("date"))
        amount = _coerce_optional_float(row.get("value"))
        if ex_date is None or amount is None or amount < 0:
            continue
        yield DividendRow(
            ticker=ticker,
            ex_date=ex_date,
            amount=amount,
            currency=row.get("currency"),
            pay_date=_parse_date(row.get("paymentDate")),
            record_date=_parse_date(row.get("recordDate")),
            declaration_date=_parse_date(row.get("declarationDate")),
        )


def parse_news_response(ticker: str, payload: Any) -> Iterator[NewsArticleRow]:
    _check_free_tier(payload)
    if not isinstance(payload, list):
        return iter(())
    for row in payload:
        if not isinstance(row, dict):
            continue
        published_at = _parse_datetime(row.get("date"))
        title = row.get("title")
        if published_at is None or not title:
            continue
        sentiment = None
        sent_obj = row.get("sentiment")
        if isinstance(sent_obj, dict):
            sentiment = _coerce_optional_float(sent_obj.get("polarity"))
        elif isinstance(sent_obj, (int, float)):
            sentiment = float(sent_obj)
        yield NewsArticleRow(
            ticker=ticker,
            published_at=published_at,
            title=title,
            url=row.get("link"),
            source_name=row.get("source") or row.get("source_name"),
            sentiment=sentiment,
        )


def parse_insider_response(ticker: str, payload: Any) -> Iterator[InsiderTransactionRow]:
    _check_free_tier(payload)
    if not isinstance(payload, list):
        return iter(())
    for row in payload:
        if not isinstance(row, dict):
            continue
        tx_date = _parse_date(row.get("transactionDate") or row.get("date"))
        if tx_date is None:
            continue
        shares = _coerce_optional_float(row.get("transactionAmount"))
        price = _coerce_optional_float(row.get("transactionPrice"))
        value = (shares * price) if shares is not None and price is not None else None
        yield InsiderTransactionRow(
            ticker=ticker,
            date=tx_date,
            owner_name=row.get("ownerName"),
            owner_relation=row.get("ownerRelationship"),
            transaction_code=row.get("transactionCode"),
            shares=shares,
            price=price,
            value=value,
            vendor_id=row.get("ownerCik"),
        )


def parse_sentiments_response(
    ticker: str, payload: Any
) -> Iterator[NewsSentimentRow]:
    """EODHD sentiment endpoint returns ``{"<ticker>": [{date, count, normalized}, ...]}``."""
    _check_free_tier(payload)
    if isinstance(payload, dict):
        rows = payload.get(ticker) or next(iter(payload.values()), [])
    elif isinstance(payload, list):
        rows = payload
    else:
        return iter(())
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        d = _parse_date(row.get("date"))
        if d is None:
            continue
        yield NewsSentimentRow(
            ticker=ticker,
            date=d,
            sentiment=_coerce_optional_float(row.get("normalized")),
            article_count=_coerce_optional_int(row.get("count")),
        )


def parse_splits_response(ticker: str, payload: Any) -> Iterator[StockSplitRow]:
    """Parse ``/api/splits/{TICKER}`` into StockSplitRow entries.

    EODHD returns ``[{"date": "YYYY-MM-DD", "split": "2.000000/1.000000"}, ...]``
    where the split string is ``"new/old"``; we emit ``ratio = new / old``
    (forward-split ratios > 1, reverse < 1).
    """
    _check_free_tier(payload)
    if not isinstance(payload, list):
        return iter(())
    for row in payload:
        if not isinstance(row, dict):
            continue
        d = _parse_date(row.get("date"))
        split_str = row.get("split")
        if d is None or not isinstance(split_str, str) or "/" not in split_str:
            continue
        try:
            new, old = split_str.split("/", 1)
            ratio = float(new) / float(old)
        except (ValueError, ZeroDivisionError):
            continue
        if ratio <= 0:
            continue
        yield StockSplitRow(ticker=ticker, date=d, ratio=ratio)


def parse_market_cap_response(ticker: str, payload: Any) -> Iterator[MarketCapRow]:
    """Parse ``/api/historical-market-cap/{TICKER}`` into MarketCapRow.

    EODHD returns a dict keyed by integer-string index
    (``{"0": {"date": "...", "value": ...}, "1": {...}, ...}``) rather
    than a list — we iterate values directly.
    """
    _check_free_tier(payload)
    if isinstance(payload, dict):
        rows = payload.values()
    elif isinstance(payload, list):
        rows = payload
    else:
        return iter(())
    for row in rows:
        if not isinstance(row, dict):
            continue
        d = _parse_date(row.get("date"))
        mcap = _coerce_optional_float(row.get("value"))
        if d is None or mcap is None or mcap < 0:
            continue
        yield MarketCapRow(ticker=ticker, date=d, market_cap=mcap)


# ---- HTTP client ------------------------------------------------------------


class EodhdDataSource(DataSource):
    source_id = "eodhd"

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://eodhd.com/api",
        timeout_seconds: int = 30,
        max_retries: int = 3,
        retry_backoff_seconds: float = 1.0,
        session: requests.Session | None = None,
    ):
        if not api_key:
            raise ValueError("EODHD api_key is required")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._max_retries = max_retries
        self._backoff = retry_backoff_seconds
        self._session = session or requests.Session()
        self._log = get_logger("stonks.ingest.sources.eodhd")

    def list_tickers(self, exchange: str) -> list[str]:
        url = f"{self._base_url}/exchange-symbol-list/{exchange}"
        data = self._get(url, params={"fmt": "json"})
        if not isinstance(data, list):
            return []
        return [f"{row['Code']}.{exchange}" for row in data if isinstance(row, dict) and "Code" in row]

    def fetch_prices(
        self, ticker: str, since: date | None = None, until: date | None = None
    ) -> Iterable[RawPriceBar]:
        url = f"{self._base_url}/eod/{ticker}"
        params: dict[str, str] = {"fmt": "json"}
        if since is not None:
            params["from"] = since.isoformat()
        if until is not None:
            params["to"] = until.isoformat()
        data = self._get(url, params=params)
        return list(parse_prices_response(ticker, data))

    def fetch_fundamentals(self, ticker: str) -> Iterable[FundamentalRow]:
        url = f"{self._base_url}/fundamentals/{ticker}"
        data = self._get(url, params={"fmt": "json"})
        return list(parse_fundamentals_response(ticker, data))

    # EODHD natively supports 1-minute, 5-minute, and 1-hour bars on the
    # /api/intraday endpoint. Everything else is derived via aggregation.
    _INTRADAY_NATIVE = frozenset({"1m", "5m", "1h"})

    def fetch_intraday_bars(
        self,
        ticker: str,
        interval: Interval,
        since: date | None = None,
        until: date | None = None,
    ) -> Iterable[IntradayBar]:
        if interval.code not in self._INTRADAY_NATIVE:
            raise ValueError(
                f"EODHD intraday supports {sorted(self._INTRADAY_NATIVE)}; "
                f"use aggregation for {interval.code!r}"
            )
        url = f"{self._base_url}/intraday/{ticker}"
        params: dict[str, str] = {"fmt": "json", "interval": interval.code}
        if since is not None:
            params["from"] = str(int(datetime(since.year, since.month, since.day, tzinfo=UTC).timestamp()))
        if until is not None:
            params["to"] = str(int(datetime(until.year, until.month, until.day, 23, 59, 59, tzinfo=UTC).timestamp()))
        data = self._get(url, params=params)
        return list(parse_intraday_response(ticker, data))

    def fetch_metadata(self, ticker: str, since: date | None = None) -> MetadataBundle:
        """Assemble the full metadata bundle by hitting five EODHD endpoints
        in parallel (``requests.Session`` is thread-safe for concurrent GETs).

        Each sub-fetch is wrapped in free-tier / network error tolerance:
        a subscription-blocked or transport error on one endpoint leaves the
        corresponding bundle field empty; other fields still populate.
        """
        since_iso = since.isoformat() if since is not None else None

        def _params_with_since(base: dict[str, str]) -> dict[str, str]:
            return {**base, "from": since_iso} if since_iso else base

        jobs = {
            "fundamentals": lambda: self._get_json(f"/fundamentals/{ticker}"),
            "dividends":    lambda: self._get_json(
                f"/div/{ticker}", _params_with_since({"fmt": "json"}),
            ),
            "splits":       lambda: self._get_json(
                f"/splits/{ticker}", _params_with_since({"fmt": "json"}),
            ),
            "market_cap":   lambda: self._get_json(
                f"/historical-market-cap/{ticker}", {"fmt": "json"},
            ),
            "news":         lambda: self._get_json(
                "/news", _params_with_since({"fmt": "json", "s": ticker}),
            ),
            "insider":      lambda: self._get_json(
                "/insider-transactions",
                _params_with_since({"fmt": "json", "code": ticker}),
            ),
            "sentiments":   lambda: self._get_json(
                "/sentiments", {"fmt": "json", "s": ticker},
            ),
        }

        with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
            futures = {name: pool.submit(self._try, fn) for name, fn in jobs.items()}
            results = {name: fut.result() for name, fut in futures.items()}

        bundle_parts: dict[str, Any] = {}
        fundamentals = results["fundamentals"]
        if fundamentals is not None:
            profile = parse_profile_from_fundamentals(ticker, fundamentals)
            if profile is not None:
                bundle_parts["profile"] = profile
            bundle_parts["analyst_estimates"] = tuple(
                parse_analyst_estimates_from_fundamentals(ticker, fundamentals)
            )
            rating = parse_analyst_ratings_from_fundamentals(ticker, fundamentals)
            if rating is not None:
                bundle_parts["analyst_ratings"] = rating
            # Full shares-outstanding history from fundamentals.outstandingShares;
            # falls back to empty tuple if the section is missing.
            bundle_parts["shares_outstanding"] = tuple(
                parse_shares_outstanding_history(ticker, fundamentals)
            )
            ec = parse_employee_count_snapshot(ticker, fundamentals)
            if ec is not None:
                bundle_parts["employee_count"] = (ec,)

        if results["dividends"] is not None:
            bundle_parts["dividends"] = tuple(
                parse_dividends_response(ticker, results["dividends"])
            )
        if results["splits"] is not None:
            bundle_parts["splits"] = tuple(
                parse_splits_response(ticker, results["splits"])
            )
        if results["market_cap"] is not None:
            bundle_parts["market_cap_history"] = tuple(
                parse_market_cap_response(ticker, results["market_cap"])
            )
        if results["news"] is not None:
            bundle_parts["news"] = tuple(parse_news_response(ticker, results["news"]))
        if results["insider"] is not None:
            bundle_parts["insider_transactions"] = tuple(
                parse_insider_response(ticker, results["insider"])
            )
        if results["sentiments"] is not None:
            bundle_parts["news_sentiment"] = tuple(
                parse_sentiments_response(ticker, results["sentiments"])
            )

        return MetadataBundle(**bundle_parts)

    def _get_json(self, path: str, params: dict[str, str] | None = None) -> Any:
        return self._get(f"{self._base_url}{path}", params=params or {"fmt": "json"})

    def _try(self, fn):
        """Run ``fn`` and swallow *known-safe* error types so one blocked
        endpoint doesn't poison the rest of the metadata bundle. Only
        ``EodhdFreeTierError`` (subscription), ``requests.RequestException``
        (transport), and JSON decode errors are suppressed — everything
        else (ValidationError, KeyError, AttributeError, TypeError) is a
        programming bug and propagates so tests can catch it."""
        try:
            return fn()
        except EodhdFreeTierError as exc:
            self._log.info("eodhd.metadata.skipped_free_tier", reason=str(exc))
            return None
        except (requests.RequestException, json.JSONDecodeError) as exc:
            self._log.warning("eodhd.metadata.skipped_error", error=str(exc))
            return None

    def _get(self, url: str, params: dict[str, str]) -> Any:
        params = {**params, "api_token": self._api_key}
        last_exc: Exception | None = None
        for attempt in range(1, self._max_retries + 1):
            try:
                response = self._session.get(url, params=params, timeout=self._timeout)
                # EODHD signals paid-only endpoints on the free tier with a 403 or a
                # plain-text error body — surface that as a domain error so the
                # pipeline can record it cleanly without retry churn.
                if response.status_code == 403:
                    raise EodhdFreeTierError(response.text.strip() or "HTTP 403 Forbidden")
                response.raise_for_status()
                text = response.text
                try:
                    return response.json()
                except ValueError:
                    if _FREE_TIER_TEXT in text.lower():
                        raise EodhdFreeTierError(text.strip()) from None
                    raise
            except EodhdFreeTierError:
                raise
            except Exception as exc:
                last_exc = exc
                self._log.warning(
                    "eodhd.request.failed",
                    url=url,
                    attempt=attempt,
                    max_retries=self._max_retries,
                    error=f"{type(exc).__name__}: {exc}",
                )
                if attempt < self._max_retries:
                    time.sleep(self._backoff * (2 ** (attempt - 1)))
        assert last_exc is not None
        raise last_exc

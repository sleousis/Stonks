"""EODHD data source.

Two parts, cleanly separable:
  - pure response parsers (``parse_prices_response`` / ``parse_fundamentals_response``)
    — unit-testable with captured fixtures, no HTTP.
  - :class:`EodhdDataSource` — the HTTP client that calls the API and hands the
    JSON to those parsers.

Free-tier responses for restricted endpoints come back as a plain-text error
message; we detect them and raise :class:`EodhdFreeTierError` so the pipeline
can record the failure cleanly.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Iterator
from datetime import date
from typing import Any

import requests

from stonks.ingest.schemas import FundamentalRow, RawPriceBar
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

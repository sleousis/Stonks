"""EODHD calendar endpoints (roadmap 20.7): pure parsers plus the request
helpers :class:`~stonks.ingest.sources.eodhd.EodhdDataSource` calls.

Endpoints (``https://eodhd.com/financial-apis/calendar-upcoming-earnings-ipos-and-splits``
and ``.../economic-events-data-api``):

- ``GET /calendar/earnings?from&to&symbols&fmt=json``: ``{"earnings": [{code,
  report_date, date, before_after_market, currency, actual, estimate,
  difference, percent}]}``. ``date`` is the fiscal period end.
- ``GET /calendar/dividends?filter[date_from]&filter[date_to]&filter[symbol]
  &page[limit]&page[offset]``: ``{"data": [{date, symbol}], "links":
  {"next"}}``. Always JSON. Only the ex-date and symbol are documented;
  amounts and other dates are read when present.
- ``GET /economic-events?from&to&country&limit&offset&fmt=json``: a list of
  ``{type, comparison, period, country, date, actual, previous, estimate,
  change, change_percentage}``. ``country`` is ISO alpha-2 and ``date`` is
  UTC. ``offset`` stops at 1000, so a long window is asked a week at a time.

IPO and split calendars exist too; splits already arrive per ticker with
the metadata ingest, and IPOs have no consumer yet.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

from stonks.ingest.calendar_schemas import (
    Comparison,
    DividendEventRow,
    EarningsEventRow,
    EconomicEventRow,
)
from stonks.ingest.sources.eodhd import (
    _BEFORE_AFTER_MARKET_MAP,
    _check_free_tier,
    _coerce_optional_float,
    _log_parse_drops,
    _log_unknown_vendor_value,
    _parse_date,
)

#: Tickers per ``/calendar/earnings`` request (keeps the URL short).
EARNINGS_SYMBOLS_PER_CALL = 50
#: Rows per page on the paged endpoints (the vendor maximum).
PAGE_LIMIT = 1000
#: Highest ``offset`` ``/economic-events`` accepts.
ECONOMIC_MAX_OFFSET = 1000
#: Days per ``/economic-events`` window.
ECONOMIC_WINDOW_DAYS = 7

_COMPARISONS: dict[str, Comparison] = {"mom": "mom", "qoq": "qoq", "yoy": "yoy"}

Getter = Callable[[str, dict[str, str]], Any]


def _text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _utc(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def parse_earnings_calendar(payload: Any) -> Iterator[EarningsEventRow]:
    """Rows of a ``/calendar/earnings`` payload. Rows without a ticker or a
    report date are dropped; a missing period end falls back to the report
    date (the key must be set)."""
    _check_free_tier(payload)
    items = payload.get("earnings") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return
    kept = dropped = 0
    try:
        for row in items:
            if not isinstance(row, dict):
                dropped += 1
                continue
            ticker = _text(row.get("code"))
            report = _parse_date(row.get("report_date"))
            if ticker is None or report is None:
                dropped += 1
                continue
            raw_timing = row.get("before_after_market")
            timing = _BEFORE_AFTER_MARKET_MAP.get(raw_timing or "")
            if timing is None and _text(raw_timing):
                _log_unknown_vendor_value("eodhd.earnings.unknown_timing", raw_timing)
            kept += 1
            yield EarningsEventRow(
                ticker=ticker,
                period_end=_parse_date(row.get("date")) or report,
                report_date=report,
                before_after_market=timing,  # type: ignore[arg-type]
                currency=_text(row.get("currency")),
                eps_actual=_coerce_optional_float(row.get("actual")),
                eps_estimate=_coerce_optional_float(row.get("estimate")),
                eps_difference=_coerce_optional_float(row.get("difference")),
                surprise_percent=_coerce_optional_float(row.get("percent")),
            )
    finally:
        _log_parse_drops("parse_earnings_calendar", "*", kept, dropped)


def _dividend_items(payload: Any) -> list[Any]:
    _check_free_tier(payload)
    items = payload.get("data") if isinstance(payload, dict) else None
    return items if isinstance(items, list) else []


def parse_dividend_calendar(payload: Any) -> Iterator[DividendEventRow]:
    """Rows of a ``/calendar/dividends`` page."""
    kept = dropped = 0
    try:
        for row in _dividend_items(payload):
            if not isinstance(row, dict):
                dropped += 1
                continue
            ticker = _text(row.get("symbol"))
            ex_date = _parse_date(row.get("date"))
            if ticker is None or ex_date is None:
                dropped += 1
                continue
            amount = _coerce_optional_float(row.get("unadjustedValue"))
            if amount is None:
                amount = _coerce_optional_float(row.get("value"))
            kept += 1
            yield DividendEventRow(
                ticker=ticker,
                ex_date=ex_date,
                amount=amount if amount is not None and amount >= 0 else None,
                currency=_text(row.get("currency")),
                record_date=_parse_date(row.get("recordDate")),
                pay_date=_parse_date(row.get("paymentDate")),
                declaration_date=_parse_date(row.get("declarationDate")),
            )
    finally:
        _log_parse_drops("parse_dividend_calendar", "*", kept, dropped)


def parse_economic_events(payload: Any) -> Iterator[EconomicEventRow]:
    """Rows of an ``/economic-events`` payload. Rows without a type, a
    country or a time are dropped; an unknown comparison reads as none."""
    _check_free_tier(payload)
    if not isinstance(payload, list):
        return
    kept = dropped = 0
    try:
        for row in payload:
            if not isinstance(row, dict):
                dropped += 1
                continue
            event_type = _text(row.get("type"))
            country = _text(row.get("country"))
            when = _utc(row.get("date"))
            if event_type is None or country is None or when is None or len(country) > 3:
                dropped += 1
                continue
            raw = (_text(row.get("comparison")) or "").lower()
            kept += 1
            yield EconomicEventRow(
                country=country,
                event_time=when,
                event_type=event_type[:200],
                comparison=_COMPARISONS.get(raw, "none"),
                period=_text(row.get("period")),
                actual=_coerce_optional_float(row.get("actual")),
                previous=_coerce_optional_float(row.get("previous")),
                estimate=_coerce_optional_float(row.get("estimate")),
                change=_coerce_optional_float(row.get("change")),
                change_pct=_coerce_optional_float(row.get("change_percentage")),
            )
    finally:
        _log_parse_drops("parse_economic_events", "*", kept, dropped)


# ---- request helpers (the client passes its own ``_get_json``) ----------------------


def _chunks(items: Sequence[str], size: int) -> Iterator[list[str]]:
    for i in range(0, len(items), size):
        yield list(items[i : i + size])


def fetch_earnings(
    get: Getter, start: date, end: date, tickers: Sequence[str] | None
) -> list[EarningsEventRow]:
    base = {"from": start.isoformat(), "to": end.isoformat(), "fmt": "json"}
    if tickers is None:
        return list(parse_earnings_calendar(get("/calendar/earnings", base)))
    out: list[EarningsEventRow] = []
    for batch in _chunks(list(tickers), EARNINGS_SYMBOLS_PER_CALL):
        payload = get("/calendar/earnings", {**base, "symbols": ",".join(batch)})
        out.extend(parse_earnings_calendar(payload))
    return out


def _dividend_pages(get: Getter, params: dict[str, str]) -> list[DividendEventRow]:
    out: list[DividendEventRow] = []
    offset = 0
    while True:
        payload = get(
            "/calendar/dividends",
            {**params, "page[limit]": str(PAGE_LIMIT), "page[offset]": str(offset)},
        )
        page = list(parse_dividend_calendar(payload))
        out.extend(page)
        if len(_dividend_items(payload)) < PAGE_LIMIT:
            return out
        offset += PAGE_LIMIT


def fetch_dividends(
    get: Getter, start: date, end: date, tickers: Sequence[str] | None
) -> list[DividendEventRow]:
    base = {"filter[date_from]": start.isoformat(), "filter[date_to]": end.isoformat()}
    if tickers is None:
        return _dividend_pages(get, base)
    out: list[DividendEventRow] = []
    for ticker in tickers:
        out.extend(_dividend_pages(get, {**base, "filter[symbol]": ticker}))
    return out


def _windows(start: date, end: date) -> Iterator[tuple[date, date]]:
    day = start
    while day <= end:
        last = min(end, day + timedelta(days=ECONOMIC_WINDOW_DAYS - 1))
        yield day, last
        day = last + timedelta(days=1)


def fetch_economic(
    get: Getter, start: date, end: date, countries: Sequence[str] | None
) -> list[EconomicEventRow]:
    out: list[EconomicEventRow] = []
    targets: list[str | None] = [c.strip().upper() for c in countries] if countries else [None]
    for country in targets:
        for lo, hi in _windows(start, end):
            params = {"from": lo.isoformat(), "to": hi.isoformat(), "fmt": "json"}
            if country:
                params["country"] = country
            offset = 0
            while offset <= ECONOMIC_MAX_OFFSET:
                payload = get(
                    "/economic-events",
                    {**params, "limit": str(PAGE_LIMIT), "offset": str(offset)},
                )
                page = list(parse_economic_events(payload))
                out.extend(page)
                if not isinstance(payload, list) or len(payload) < PAGE_LIMIT:
                    break
                offset += PAGE_LIMIT
    return out

"""EODHD data source.

Two parts, cleanly separable:
  - pure response parsers (``parse_*_response`` / ``parse_*_from_fundamentals``)
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

Vendor-specific vocabulary is normalized at parse time — see the
``_BEFORE_AFTER_MARKET_MAP``, ``_PERIOD_RELATIVE_MAP``, and
``_SECURITY_TYPE_MAP`` translations.

Things to keep in mind
----------------------
- **Full API catalog**: ``https://eodhd.com/financial-apis/`` is the vendor's
  top-level index of every endpoint they expose (prices, fundamentals, news,
  options, macro, crypto, screener, websockets, …). Each tile links to the
  per-endpoint docs page with the URL shape, query params, response example,
  and credit cost. When extending this adapter — or evaluating whether
  EODHD covers a new domain we want to ingest — start here rather than
  guessing endpoint paths or scraping individual articles in isolation.
- **Bulk EOD / splits / dividends endpoint** (not yet wired up):
  ``https://eodhd.com/api/eod-bulk-last-day/{EXCHANGE}`` returns one
  full-exchange snapshot per request (optionally ``?type=splits`` or
  ``?type=dividends``). Cost is a flat 100 API calls for the whole
  exchange vs. 1 per ticker on the per-symbol endpoints, so for daily
  market-wide refreshes it is dramatically cheaper than looping over
  ``fetch_prices`` / ``fetch_dividends`` / ``fetch_splits`` per ticker.
  Caveats: the ``symbols=`` filter only applies to EOD (splits/dividends
  ignore it), ``filter=extended`` data only covers the last 30 days, and
  it is a single-day snapshot — backfilling history still needs the
  per-ticker endpoints. Skipped for now to avoid adding a parallel
  ingestion path; revisit when daily full-universe refreshes become a
  bottleneck.
- **Bulk fundamentals endpoints** (not yet wired up): EODHD also exposes
  exchange-wide bulk variants for fundamentals (and related metadata) that
  return one snapshot covering the whole exchange per call rather than one
  call per ticker. Same trade-off as the bulk EOD endpoint above — much
  cheaper for full-universe refreshes, but adds a parallel ingestion path.
  Worth revisiting if per-ticker ``fetch_fundamentals`` / ``fetch_metadata``
  fan-out becomes the dominant cost as the universe grows.
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
    AnalystForecastRow,
    AnalystRatingsRow,
    CrossListingRow,
    DividendRow,
    EarningsAnnouncementRow,
    EmployeeCountRow,
    EsgActivityRow,
    EsgSnapshotRow,
    ExchangeInfo,
    FundamentalRow,
    InsiderTransactionRow,
    InstitutionalHolderRow,
    IntradayBar,
    MarketCapRow,
    NewsArticleRow,
    NewsSentimentRow,
    OfficerRow,
    RawPriceBar,
    SharesOutstandingRow,
    StockSplitRow,
    TickerProfile,
    TickerSnapshotRow,
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

# Vendor-string → canonical literal maps (vendor-agnostic principle: the
# lake never sees vendor vocabulary).
_BEFORE_AFTER_MARKET_MAP = {
    "BeforeMarket": "before",
    "AfterMarket": "after",
    "DuringMarket": "during",
}
_PERIOD_RELATIVE_MAP = {
    "-1y": "prior_year",
    "-1q": "prior_quarter",
    "0q": "current_quarter",
    "+1q": "next_quarter",
    "0y": "current_year",
    "+1y": "next_year",
}
# EODHD's `General.Type` strings vary in capitalization; we normalize on
# lower-cased contains-checks. Anything that doesn't match falls into "other".
_SECURITY_TYPE_KEYWORDS = (
    ("preferred", "preferred_stock"),
    ("adr", "adr"),
    ("etf", "etf"),
    ("fund", "fund"),
    ("common", "common_stock"),
)


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


def _str_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str) and item)


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


def _normalize_security_type(raw: Any) -> str | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    needle = raw.strip().lower()
    for keyword, canonical in _SECURITY_TYPE_KEYWORDS:
        if keyword in needle:
            return canonical
    return "other"


# ---- extended-fundamentals parsers (pure, unit-tested) ---------------------


def parse_profile_from_fundamentals(ticker: str, payload: Any) -> TickerProfile | None:
    """Extract the static :class:`TickerProfile` from the fundamentals JSON.

    Volatile metrics (beta, short interest, ownership %) are routed to
    :class:`TickerSnapshotRow` via :func:`parse_ticker_snapshot_from_fundamentals`.
    """
    if not isinstance(payload, dict):
        return None
    general = payload.get("General") or {}
    address_data = general.get("AddressData") or {}

    return TickerProfile(
        id=ticker,
        exchange=general.get("Exchange"),
        currency=general.get("CurrencyCode"),
        name=general.get("Name"),
        country_iso=general.get("CountryISO"),
        sector=general.get("Sector"),
        industry=general.get("Industry"),
        gic_sector=general.get("GicSector"),
        gic_group=general.get("GicGroup"),
        gic_industry=general.get("GicIndustry"),
        gic_sub_industry=general.get("GicSubIndustry"),
        ipo_date=_parse_date(general.get("IPODate")),
        is_delisted=bool(general.get("IsDelisted", False)),
        delisted_date=_parse_date(general.get("DelistedDate")),
        is_bank=bool(general.get("IsBank", False)),
        fiscal_year_end=general.get("FiscalYearEnd"),
        security_type=_normalize_security_type(general.get("Type")),
        cusip=general.get("CUSIP"),
        cik=general.get("CIK"),
        isin=general.get("ISIN"),
        open_figi=general.get("OpenFigi"),
        lei=general.get("LEI"),
        employer_id_number=general.get("EmployerIdNumber"),
        primary_ticker=general.get("PrimaryTicker"),
        address_street=address_data.get("Street"),
        address_city=address_data.get("City"),
        address_state=address_data.get("State"),
        address_country=address_data.get("Country"),
        address_zip=address_data.get("ZIP"),
        phone=general.get("Phone"),
        web_url=general.get("WebURL"),
        description=general.get("Description"),
        updated_at=_parse_datetime(general.get("UpdatedAt")),
    )


def parse_ticker_snapshot_from_fundamentals(
    ticker: str, payload: Any, as_of: date | None = None
) -> TickerSnapshotRow | None:
    """Extract the volatile per-ticker metrics into a snapshot row.

    Vendors don't always carry an explicit "as of" date for these fields;
    callers can override via ``as_of``, otherwise we use today's date so
    the row has a valid time-series anchor.
    """
    if not isinstance(payload, dict):
        return None
    shares_stats = payload.get("SharesStats") or {}
    technicals = payload.get("Technicals") or {}

    snapshot_date = as_of or date.today()
    beta = _coerce_optional_float(technicals.get("Beta"))
    short_percent = _coerce_optional_float(
        shares_stats.get("ShortPercentFloat")
        or shares_stats.get("ShortPercent")
        or technicals.get("ShortPercent")
    )
    percent_insiders = _coerce_optional_float(shares_stats.get("PercentInsiders"))
    percent_institutions = _coerce_optional_float(shares_stats.get("PercentInstitutions"))

    if all(v is None for v in (beta, short_percent, percent_insiders, percent_institutions)):
        return None

    return TickerSnapshotRow(
        ticker=ticker,
        snapshot_date=snapshot_date,
        beta=beta,
        short_percent=short_percent,
        percent_insiders=percent_insiders,
        percent_institutions=percent_institutions,
    )


def parse_earnings_announcements_from_fundamentals(
    ticker: str, payload: Any
) -> Iterator[EarningsAnnouncementRow]:
    """Parse ``Earnings.History`` into structured earnings events.

    Replaces the older flat ``AnalystEstimateRow(metric, value)`` shape: the
    structured form preserves the announcement timing (``report_date`` /
    ``before_after_market``) and pairs actual + estimate + surprise on the
    same row.
    """
    if not isinstance(payload, dict):
        return iter(())
    history = ((payload.get("Earnings") or {}).get("History")) or {}
    return _iter_earnings_announcements(ticker, history)


def _iter_earnings_announcements(ticker: str, history: dict) -> Iterator[EarningsAnnouncementRow]:
    for period_key, row in history.items():
        if not isinstance(row, dict):
            continue
        period_end = _parse_date(row.get("date") or period_key)
        if period_end is None:
            continue
        yield EarningsAnnouncementRow(
            ticker=ticker,
            period_end=period_end,
            report_date=_parse_date(row.get("reportDate")),
            before_after_market=_BEFORE_AFTER_MARKET_MAP.get(row.get("beforeAfterMarket") or ""),
            currency=row.get("currency"),
            eps_actual=_coerce_optional_float(row.get("epsActual")),
            eps_estimate=_coerce_optional_float(row.get("epsEstimate")),
            eps_difference=_coerce_optional_float(row.get("epsDifference")),
            surprise_percent=_coerce_optional_float(row.get("surprisePercent")),
        )


def parse_analyst_forecasts_from_fundamentals(
    ticker: str, payload: Any
) -> Iterator[AnalystForecastRow]:
    """Parse ``Earnings.Trend`` into analyst dispersion + revision rows.

    Vendor strings ``-1q`` / ``0q`` / ``+1q`` / ``-1y`` / ``0y`` / ``+1y``
    map to the canonical ``period_relative`` literal.
    """
    if not isinstance(payload, dict):
        return iter(())
    trend = ((payload.get("Earnings") or {}).get("Trend")) or {}
    return _iter_analyst_forecasts(ticker, trend)


def _iter_analyst_forecasts(ticker: str, trend: dict) -> Iterator[AnalystForecastRow]:
    for period_key, row in trend.items():
        if not isinstance(row, dict):
            continue
        period_end = _parse_date(row.get("date") or period_key)
        if period_end is None:
            continue
        period_relative = _PERIOD_RELATIVE_MAP.get(row.get("period") or "")
        yield AnalystForecastRow(
            ticker=ticker,
            period_end=period_end,
            period_relative=period_relative,
            growth=_coerce_optional_float(row.get("growth")),
            eps_estimate_avg=_coerce_optional_float(row.get("earningsEstimateAvg")),
            eps_estimate_low=_coerce_optional_float(row.get("earningsEstimateLow")),
            eps_estimate_high=_coerce_optional_float(row.get("earningsEstimateHigh")),
            eps_estimate_year_ago=_coerce_optional_float(row.get("earningsEstimateYearAgoEps")),
            eps_estimate_n_analysts=_coerce_optional_int(
                row.get("earningsEstimateNumberOfAnalysts")
            ),
            eps_estimate_growth=_coerce_optional_float(row.get("earningsEstimateGrowth")),
            revenue_estimate_avg=_coerce_optional_float(row.get("revenueEstimateAvg")),
            revenue_estimate_low=_coerce_optional_float(row.get("revenueEstimateLow")),
            revenue_estimate_high=_coerce_optional_float(row.get("revenueEstimateHigh")),
            revenue_estimate_year_ago=_coerce_optional_float(row.get("revenueEstimateYearAgoEps")),
            revenue_estimate_n_analysts=_coerce_optional_int(
                row.get("revenueEstimateNumberOfAnalysts")
            ),
            revenue_estimate_growth=_coerce_optional_float(row.get("revenueEstimateGrowth")),
            eps_trend_current=_coerce_optional_float(row.get("epsTrendCurrent")),
            eps_trend_7d_ago=_coerce_optional_float(row.get("epsTrend7daysAgo")),
            eps_trend_30d_ago=_coerce_optional_float(row.get("epsTrend30daysAgo")),
            eps_trend_60d_ago=_coerce_optional_float(row.get("epsTrend60daysAgo")),
            eps_trend_90d_ago=_coerce_optional_float(row.get("epsTrend90daysAgo")),
            eps_revisions_up_7d=_coerce_optional_int(row.get("epsRevisionsUpLast7days")),
            eps_revisions_up_30d=_coerce_optional_int(row.get("epsRevisionsUpLast30days")),
            eps_revisions_down_7d=_coerce_optional_int(row.get("epsRevisionsDownLast7days")),
            eps_revisions_down_30d=_coerce_optional_int(row.get("epsRevisionsDownLast30days")),
        )


def parse_analyst_ratings_from_fundamentals(
    ticker: str, payload: Any, as_of: date | None = None
) -> AnalystRatingsRow | None:
    """Extract the consensus ratings snapshot.

    The row carries an explicit ``snapshot_date`` so the table is a proper
    time series; callers pass ``as_of`` (or we fall back to today) since
    EODHD doesn't expose a per-snapshot timestamp on this object.
    """
    if not isinstance(payload, dict):
        return None
    ratings = payload.get("AnalystRatings")
    if not isinstance(ratings, dict):
        return None
    return AnalystRatingsRow(
        ticker=ticker,
        snapshot_date=as_of or date.today(),
        rating=_coerce_optional_float(ratings.get("Rating")),
        target_price=_coerce_optional_float(ratings.get("TargetPrice")),
        strong_buy=_coerce_optional_int(ratings.get("StrongBuy")) or 0,
        buy=_coerce_optional_int(ratings.get("Buy")) or 0,
        hold=_coerce_optional_int(ratings.get("Hold")) or 0,
        sell=_coerce_optional_int(ratings.get("Sell")) or 0,
        strong_sell=_coerce_optional_int(ratings.get("StrongSell")) or 0,
    )


def parse_holders_from_fundamentals(ticker: str, payload: Any) -> Iterator[InstitutionalHolderRow]:
    """Parse ``Holders.Institutions`` and ``Holders.Funds`` into rows.

    Both sub-objects share the same shape; the ``holder_kind``
    discriminator distinguishes them downstream.
    """
    if not isinstance(payload, dict):
        return iter(())
    holders = payload.get("Holders")
    if not isinstance(holders, dict):
        return iter(())
    return _iter_holders(ticker, holders)


def _iter_holders(ticker: str, holders: dict) -> Iterator[InstitutionalHolderRow]:
    for vendor_kind, canonical_kind in (("Institutions", "institution"), ("Funds", "fund")):
        bucket = holders.get(vendor_kind)
        if not isinstance(bucket, dict):
            continue
        for entry in bucket.values():
            if not isinstance(entry, dict):
                continue
            name = entry.get("name")
            snapshot_date = _parse_date(entry.get("date"))
            if not name or snapshot_date is None:
                continue
            yield InstitutionalHolderRow(
                ticker=ticker,
                holder_kind=canonical_kind,  # type: ignore[arg-type]
                name=name,
                snapshot_date=snapshot_date,
                total_shares_pct=_coerce_optional_float(entry.get("totalShares")),
                total_assets_pct=_coerce_optional_float(entry.get("totalAssets")),
                current_shares=_coerce_optional_int(entry.get("currentShares")),
                change_shares=_coerce_optional_int(entry.get("change")),
                change_pct=_coerce_optional_float(entry.get("change_p")),
            )


def parse_esg_from_fundamentals(
    ticker: str, payload: Any
) -> tuple[EsgSnapshotRow | None, list[EsgActivityRow]]:
    """Parse ``ESGScores`` into a header row plus per-activity child rows."""
    if not isinstance(payload, dict):
        return (None, [])
    esg = payload.get("ESGScores")
    if not isinstance(esg, dict):
        return (None, [])
    rating_date = _parse_date(esg.get("RatingDate"))
    if rating_date is None:
        return (None, [])
    snapshot = EsgSnapshotRow(
        ticker=ticker,
        rating_date=rating_date,
        total_esg=_coerce_optional_float(esg.get("TotalEsg") or esg.get("TotalESG")),
        total_esg_percentile=_coerce_optional_float(esg.get("TotalEsgPercentile")),
        environment_score=_coerce_optional_float(esg.get("EnvironmentScore")),
        environment_percentile=_coerce_optional_float(esg.get("EnvironmentScorePercentile")),
        social_score=_coerce_optional_float(esg.get("SocialScore")),
        social_percentile=_coerce_optional_float(esg.get("SocialScorePercentile")),
        governance_score=_coerce_optional_float(esg.get("GovernanceScore")),
        governance_percentile=_coerce_optional_float(esg.get("GovernanceScorePercentile")),
        controversy_level=_coerce_optional_int(esg.get("ControversyLevel")),
    )
    activities_raw = esg.get("ActivitiesInvolvement")
    activities: list[EsgActivityRow] = []
    if isinstance(activities_raw, dict):
        for entry in activities_raw.values():
            if not isinstance(entry, dict):
                continue
            activity = entry.get("Activity")
            involvement = entry.get("Involvement")
            if not activity or involvement is None:
                continue
            activities.append(
                EsgActivityRow(
                    ticker=ticker,
                    rating_date=rating_date,
                    activity=str(activity),
                    involvement=str(involvement),
                )
            )
    return (snapshot, activities)


def parse_cross_listings_from_fundamentals(ticker: str, payload: Any) -> Iterator[CrossListingRow]:
    """Parse ``General.Listings`` (other-exchange listings) into rows."""
    if not isinstance(payload, dict):
        return iter(())
    listings = (payload.get("General") or {}).get("Listings")
    if not isinstance(listings, dict):
        return iter(())
    for entry in listings.values():
        if not isinstance(entry, dict):
            continue
        exchange = entry.get("Exchange")
        code = entry.get("Code")
        if not exchange or not code:
            continue
        yield CrossListingRow(
            ticker=ticker,
            exchange=exchange,
            exchange_code=code,
            name=entry.get("Name"),
        )


def parse_officers_from_fundamentals(ticker: str, payload: Any) -> Iterator[OfficerRow]:
    """Parse the current officer roster from ``General.Officers``."""
    if not isinstance(payload, dict):
        return iter(())
    officers = (payload.get("General") or {}).get("Officers")
    if not isinstance(officers, dict):
        return iter(())
    for entry in officers.values():
        if not isinstance(entry, dict):
            continue
        name = entry.get("Name")
        if not name:
            continue
        yield OfficerRow(
            ticker=ticker,
            name=name,
            title=entry.get("Title"),
            year_born=_coerce_optional_int(entry.get("YearBorn")),
        )


def parse_shares_outstanding_history(ticker: str, payload: Any) -> Iterator[SharesOutstandingRow]:
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


def _iter_shares_outstanding(ticker: str, shares_blob: dict) -> Iterator[SharesOutstandingRow]:
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
        sentiment_polarity: float | None = None
        sentiment_pos: float | None = None
        sentiment_neg: float | None = None
        sentiment_neu: float | None = None
        sent_obj = row.get("sentiment")
        if isinstance(sent_obj, dict):
            sentiment_polarity = _coerce_optional_float(sent_obj.get("polarity"))
            sentiment_pos = _coerce_optional_float(sent_obj.get("pos"))
            sentiment_neg = _coerce_optional_float(sent_obj.get("neg"))
            sentiment_neu = _coerce_optional_float(sent_obj.get("neu"))
        elif isinstance(sent_obj, (int, float)):
            sentiment_polarity = float(sent_obj)
        yield NewsArticleRow(
            ticker=ticker,
            published_at=published_at,
            title=title,
            url=row.get("link"),
            source_name=row.get("source") or row.get("source_name"),
            # `content` is deliberately not populated: storing every article
            # body would meaningfully inflate the lake without a current
            # consumer. The schema column exists so a future flip can begin
            # populating it without a migration.
            content=None,
            symbols=_str_tuple(row.get("symbols")),
            tags=_str_tuple(row.get("tags")),
            sentiment=sentiment_polarity,
            sentiment_pos=sentiment_pos,
            sentiment_neg=sentiment_neg,
            sentiment_neu=sentiment_neu,
        )


def parse_insider_response(ticker: str, payload: Any) -> Iterator[InsiderTransactionRow]:
    """Parse ``/api/insider-transactions`` rows.

    Vendor field mapping:
      - ``transactionDate`` → ``transaction_date`` (actual trade date)
      - ``reportDate`` → ``filing_date`` (SEC filing date; lag = signal)
      - ``ownerCik`` → ``owner_cik``
      - ``ownerRelationship`` → ``owner_relation`` (categorical)
      - ``ownerTitle`` → ``owner_title`` (free text e.g. "CEO")
      - ``transactionAcquiredDisposed`` → ``acquired_disposed`` (A | D)
      - ``postTransactionAmount`` → ``post_transaction_amount``
      - ``link`` → ``sec_link`` (URL to the underlying Form 4)

    Derives ``value = shares * price`` when both are present.
    """
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
        ad_raw = row.get("transactionAcquiredDisposed")
        acquired_disposed = ad_raw if ad_raw in ("A", "D") else None
        yield InsiderTransactionRow(
            ticker=ticker,
            transaction_date=tx_date,
            filing_date=_parse_date(row.get("reportDate")),
            owner_name=row.get("ownerName"),
            owner_cik=row.get("ownerCik"),
            owner_relation=row.get("ownerRelationship"),
            owner_title=row.get("ownerTitle"),
            transaction_code=row.get("transactionCode"),
            acquired_disposed=acquired_disposed,
            shares=shares,
            price=price,
            value=value,
            post_transaction_amount=_coerce_optional_float(row.get("postTransactionAmount")),
            sec_link=row.get("link") or row.get("secLink"),
        )


def parse_sentiments_response(ticker: str, payload: Any) -> Iterator[NewsSentimentRow]:
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


def parse_exchanges_response(payload: Any) -> Iterator[ExchangeInfo]:
    """Parse ``/api/exchanges-list`` rows into :class:`ExchangeInfo`.

    Vendor field mapping (PascalCase → canonical snake_case):
      ``Code → code``, ``Name → name``, ``Country → country``,
      ``Currency → currency``, ``CountryISO2 → country_iso2``,
      ``CountryISO3 → country_iso3``, ``OperatingMIC → operating_mic``.

    Rows missing ``Code`` are skipped (no usable identifier). The vendor's
    response includes virtual asset-class buckets (FOREX, CC, INDX, …)
    alongside real stock exchanges; both are passed through — callers
    decide whether to filter.
    """
    _check_free_tier(payload)
    if not isinstance(payload, list):
        return iter(())
    return _iter_exchanges(payload)


def _iter_exchanges(payload: list) -> Iterator[ExchangeInfo]:
    for row in payload:
        if not isinstance(row, dict):
            continue
        code = row.get("Code")
        if not code:
            continue
        yield ExchangeInfo(
            code=code,
            name=row.get("Name"),
            country=row.get("Country"),
            currency=row.get("Currency"),
            country_iso2=row.get("CountryISO2"),
            country_iso3=row.get("CountryISO3"),
            operating_mic=row.get("OperatingMIC"),
        )


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

    def list_exchanges(self) -> list[ExchangeInfo]:
        url = f"{self._base_url}/exchanges-list"
        data = self._get(url, params={"fmt": "json"})
        return list(parse_exchanges_response(data))

    def list_tickers(self, exchange: str) -> list[str]:
        # EODHD's `delisted=1` returns *only* delisted entries (verified
        # empirically: the active-only and delisted=1 sets are disjoint), so
        # to surface the full survivorship-bias-free universe we issue both
        # calls and concatenate. Order is preserved (active first, then
        # delisted) and duplicates are dropped defensively in case the vendor
        # ever changes the semantics.
        url = f"{self._base_url}/exchange-symbol-list/{exchange}"
        active = self._get(url, params={"fmt": "json"})
        delisted = self._get(url, params={"fmt": "json", "delisted": "1"})
        out: list[str] = []
        seen: set[str] = set()
        for data in (active, delisted):
            if not isinstance(data, list):
                continue
            for row in data:
                if not isinstance(row, dict) or "Code" not in row:
                    continue
                ticker = f"{row['Code']}.{exchange}"
                if ticker in seen:
                    continue
                seen.add(ticker)
                out.append(ticker)
        return out

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
            params["from"] = str(
                int(datetime(since.year, since.month, since.day, tzinfo=UTC).timestamp())
            )
        if until is not None:
            params["to"] = str(
                int(
                    datetime(until.year, until.month, until.day, 23, 59, 59, tzinfo=UTC).timestamp()
                )
            )
        data = self._get(url, params=params)
        return list(parse_intraday_response(ticker, data))

    def fetch_metadata(self, ticker: str, since: date | None = None) -> MetadataBundle:
        """Assemble the full metadata bundle by hitting seven EODHD endpoints
        in parallel (``requests.Session`` is thread-safe for concurrent GETs).

        Each sub-fetch is wrapped in free-tier / network error tolerance:
        a subscription-blocked or transport error on one endpoint leaves the
        corresponding bundle field empty; other fields still populate.

        The single ``/fundamentals`` payload contributes the lion's share —
        profile, ticker_snapshot, holders, earnings_announcements,
        analyst_forecasts, analyst_ratings, esg_snapshot/activities,
        cross_listings, officers, shares_outstanding history, employee_count.
        """
        since_iso = since.isoformat() if since is not None else None

        def _params_with_since(base: dict[str, str]) -> dict[str, str]:
            return {**base, "from": since_iso} if since_iso else base

        jobs = {
            "fundamentals": lambda: self._get_json(f"/fundamentals/{ticker}"),
            "dividends": lambda: self._get_json(
                f"/div/{ticker}",
                _params_with_since({"fmt": "json"}),
            ),
            "splits": lambda: self._get_json(
                f"/splits/{ticker}",
                _params_with_since({"fmt": "json"}),
            ),
            "market_cap": lambda: self._get_json(
                f"/historical-market-cap/{ticker}",
                {"fmt": "json"},
            ),
            "news": lambda: self._get_json(
                "/news",
                _params_with_since({"fmt": "json", "s": ticker}),
            ),
            "insider": lambda: self._get_json(
                "/insider-transactions",
                _params_with_since({"fmt": "json", "code": ticker}),
            ),
            "sentiments": lambda: self._get_json(
                "/sentiments",
                {"fmt": "json", "s": ticker},
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
            snapshot = parse_ticker_snapshot_from_fundamentals(ticker, fundamentals)
            if snapshot is not None:
                bundle_parts["ticker_snapshot"] = snapshot
            bundle_parts["earnings_announcements"] = tuple(
                parse_earnings_announcements_from_fundamentals(ticker, fundamentals)
            )
            bundle_parts["analyst_forecasts"] = tuple(
                parse_analyst_forecasts_from_fundamentals(ticker, fundamentals)
            )
            rating = parse_analyst_ratings_from_fundamentals(ticker, fundamentals)
            if rating is not None:
                bundle_parts["analyst_ratings"] = (rating,)
            bundle_parts["institutional_holders"] = tuple(
                parse_holders_from_fundamentals(ticker, fundamentals)
            )
            esg_snap, esg_activities = parse_esg_from_fundamentals(ticker, fundamentals)
            if esg_snap is not None:
                bundle_parts["esg_snapshot"] = esg_snap
            if esg_activities:
                bundle_parts["esg_activities"] = tuple(esg_activities)
            bundle_parts["cross_listings"] = tuple(
                parse_cross_listings_from_fundamentals(ticker, fundamentals)
            )
            bundle_parts["officers"] = tuple(parse_officers_from_fundamentals(ticker, fundamentals))
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
            bundle_parts["splits"] = tuple(parse_splits_response(ticker, results["splits"]))
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

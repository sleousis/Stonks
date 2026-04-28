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
from typing import Any, Literal

import requests
from pydantic import ValidationError

from stonks.core.interval import Interval
from stonks.core.types import AssetClass
from stonks.ingest.metadata_bundle import MetadataBundle
from stonks.ingest.schemas import (
    AnalystForecastRow,
    AnalystRatingsRow,
    BalanceSheetRow,
    BondIssuerKind,
    BondKind,
    BondProfileRow,
    CashFlowStatementRow,
    CommodityContractKind,
    CommodityContractRow,
    CrossListingRow,
    CryptoConsensusType,
    CryptoProfileRow,
    DividendRow,
    EarningsAnnouncementRow,
    EmployeeCountRow,
    EsgActivityRow,
    EsgSnapshotRow,
    ExchangeInfo,
    FinancialStatementsBundle,
    IncomeStatementRow,
    InsiderTransactionRow,
    InstitutionalHolderRow,
    IntradayBar,
    MarketCapRow,
    NewsArticleRow,
    NewsSentimentRow,
    OfficerRow,
    RawPriceBar,
    SharesOutstandingRow,
    StatementFrequency,
    StockSplitRow,
    TickerProfile,
    TickerSnapshotRow,
)
from stonks.ingest.sources.base import DataSource, DataSourceError
from stonks.logging import get_logger

# Substrings (case-insensitive) that EODHD returns in plain-text bodies when
# the caller's free tier blocks an endpoint. Add new markers here as we
# encounter them — keeping a tuple lets ``_check_free_tier`` map any of them
# to the same domain exception without reshaping callers.
_FREE_TIER_MARKERS: tuple[str, ...] = (
    "only eod data allowed",
    "demo api",
    "this api endpoint is paid only",
)
_FREE_TIER_WARNING_KEY = "warning"

# EODHD's frequency keys → canonical literal. Iteration order matches the
# vendor's two sub-objects under each statement section.
_FREQUENCY_MAP: dict[str, StatementFrequency] = {"quarterly": "Q", "yearly": "A"}

# Vendor camelCase line item → canonical snake_case column. One map per
# statement; the map is the single source of truth that pairs the EODHD
# field names with our schema fields. Keys not present in the map are
# silently skipped at parse time (vendor adds new fields all the time —
# they land in the lake only after we've consciously added a column).
_INCOME_LINE_ITEMS: dict[str, str] = {
    "totalRevenue": "revenue",
    "costOfRevenue": "cost_of_revenue",
    "grossProfit": "gross_profit",
    "researchDevelopment": "research_development",
    "sellingGeneralAdministrative": "selling_general_administrative",
    "sellingAndMarketingExpenses": "selling_marketing_expenses",
    "otherOperatingExpenses": "other_operating_expenses",
    "totalOperatingExpenses": "total_operating_expenses",
    "operatingIncome": "operating_income",
    "interestIncome": "interest_income",
    "interestExpense": "interest_expense",
    "netInterestIncome": "net_interest_income",
    "nonOperatingIncomeNetOther": "non_operating_income_other",
    "totalOtherIncomeExpenseNet": "total_other_income_expense_net",
    "incomeBeforeTax": "income_before_tax",
    "incomeTaxExpense": "income_tax_expense",
    "taxProvision": "tax_provision",
    "minorityInterest": "minority_interest",
    "netIncomeFromContinuingOps": "net_income_continuing",
    "discontinuedOperations": "discontinued_operations",
    "extraordinaryItems": "extraordinary_items",
    "nonRecurring": "non_recurring",
    "otherItems": "other_items",
    "effectOfAccountingCharges": "effect_of_accounting_charges",
    "netIncome": "net_income",
    "netIncomeApplicableToCommonShares": "net_income_to_common",
    "preferredStockAndOtherAdjustments": "preferred_stock_adjustments",
    "ebit": "ebit",
    "ebitda": "ebitda",
    "depreciationAndAmortization": "depreciation_amortization",
    "reconciledDepreciation": "reconciled_depreciation",
}
_BALANCE_LINE_ITEMS: dict[str, str] = {
    "totalAssets": "total_assets",
    "totalCurrentAssets": "current_assets",
    "cash": "cash",
    "cashAndEquivalents": "cash_and_equivalents",
    "cashAndShortTermInvestments": "cash_and_short_term_investments",
    "shortTermInvestments": "short_term_investments",
    "netReceivables": "net_receivables",
    "inventory": "inventory",
    "otherCurrentAssets": "other_current_assets",
    "nonCurrentAssetsTotal": "non_current_assets",
    "longTermInvestments": "long_term_investments",
    "propertyPlantAndEquipmentNet": "property_plant_equipment_net",
    "propertyPlantAndEquipmentGross": "property_plant_equipment_gross",
    "accumulatedDepreciation": "accumulated_depreciation",
    "accumulatedAmortization": "accumulated_amortization",
    "goodWill": "goodwill",
    "intangibleAssets": "intangible_assets",
    "otherAssets": "other_assets",
    "deferredLongTermAssetCharges": "deferred_long_term_asset_charges",
    # EODHD's misspelling — kept verbatim on the vendor side, mapped to a
    # correctly-spelled column. The corrected spelling is also accepted
    # so an upstream typo fix doesn't silently zero the column.
    "nonCurrrentAssetsOther": "non_current_assets_other",
    "nonCurrentAssetsOther": "non_current_assets_other",
    "earningAssets": "earning_assets",
    "totalLiab": "total_liabilities",
    "totalCurrentLiabilities": "current_liabilities",
    "accountsPayable": "accounts_payable",
    "currentDeferredRevenue": "current_deferred_revenue",
    "shortTermDebt": "short_term_debt",
    "shortLongTermDebt": "short_long_term_debt",
    "shortLongTermDebtTotal": "short_long_term_debt_total",
    "otherCurrentLiab": "other_current_liabilities",
    "nonCurrentLiabilitiesTotal": "non_current_liabilities",
    "longTermDebt": "long_term_debt",
    "longTermDebtTotal": "long_term_debt_total",
    "capitalLeaseObligations": "capital_lease_obligations",
    "deferredLongTermLiab": "deferred_long_term_liabilities",
    "otherLiab": "other_liabilities",
    "nonCurrentLiabilitiesOther": "non_current_liabilities_other",
    "negativeGoodwill": "negative_goodwill",
    "warrants": "warrants",
    "preferredStockRedeemable": "preferred_stock_redeemable",
    "totalStockholderEquity": "total_stockholder_equity",
    "commonStock": "common_stock",
    "capitalStock": "capital_stock",
    "additionalPaidInCapital": "additional_paid_in_capital",
    "retainedEarnings": "retained_earnings",
    "treasuryStock": "treasury_stock",
    "accumulatedOtherComprehensiveIncome": "accumulated_other_comprehensive_income",
    "otherStockholderEquity": "other_stockholder_equity",
    "commonStockTotalEquity": "common_stock_total_equity",
    "preferredStockTotalEquity": "preferred_stock_total_equity",
    "retainedEarningsTotalEquity": "retained_earnings_total_equity",
    # EODHD's misspelling — kept verbatim on the vendor side. The
    # corrected spelling is also accepted so an upstream typo fix
    # doesn't silently zero the column.
    "capitalSurpluse": "capital_surplus",
    "capitalSurplus": "capital_surplus",
    "totalPermanentEquity": "total_permanent_equity",
    "noncontrollingInterestInConsolidatedEntity": "noncontrolling_interest",
    "temporaryEquityRedeemableNoncontrollingInterests": "temporary_equity_redeemable_noncontrolling",
    "liabilitiesAndStockholdersEquity": "liabilities_and_stockholders_equity",
    "netDebt": "net_debt",
    "netTangibleAssets": "net_tangible_assets",
    "netWorkingCapital": "net_working_capital",
    "investments": "investments",
    "commonStockSharesOutstanding": "common_stock_shares_outstanding",
}
_CASHFLOW_LINE_ITEMS: dict[str, str] = {
    "totalCashFromOperatingActivities": "operating_cash_flow",
    "totalCashflowsFromInvestingActivities": "investing_cash_flow",
    "totalCashFromFinancingActivities": "financing_cash_flow",
    "netIncome": "net_income",
    "depreciation": "depreciation",
    "stockBasedCompensation": "stock_based_compensation",
    "changeInWorkingCapital": "change_in_working_capital",
    "changeToInventory": "change_to_inventory",
    "changeToAccountReceivables": "change_to_account_receivables",
    "changeToLiabilities": "change_to_liabilities",
    "changeToOperatingActivities": "change_to_operating_activities",
    "changeToNetincome": "change_to_net_income",
    "changeReceivables": "change_receivables",
    "cashFlowsOtherOperating": "cash_flows_other_operating",
    "otherNonCashItems": "other_non_cash_items",
    "capitalExpenditures": "capital_expenditures",
    "investments": "investments",
    "otherCashflowsFromInvestingActivities": "other_cash_flows_investing",
    "dividendsPaid": "dividends_paid",
    "netBorrowings": "net_borrowings",
    "issuanceOfCapitalStock": "issuance_of_capital_stock",
    "salePurchaseOfStock": "sale_purchase_of_stock",
    "otherCashflowsFromFinancingActivities": "other_cash_flows_financing",
    "changeInCash": "change_in_cash",
    "cashAndCashEquivalentsChanges": "cash_and_cash_equivalents_changes",
    "beginPeriodCashFlow": "begin_period_cash_flow",
    "endPeriodCashFlow": "end_period_cash_flow",
    "exchangeRateChanges": "exchange_rate_changes",
    "freeCashFlow": "free_cash_flow",
}

# Vendor-string → canonical literal maps (vendor-agnostic principle: the
# lake never sees vendor vocabulary).

# EODHD ESG ``Involvement`` is a closed Yes/No set — lower-case it to match
# the schema's ``Literal["yes", "no"]``. Anything else gets dropped at the
# parse boundary (logged once per ticker).
_INVOLVEMENT_MAP: dict[str, Literal["yes", "no"]] = {
    "yes": "yes",
    "y": "yes",
    "true": "yes",
    "no": "no",
    "n": "no",
    "false": "no",
}

# Owner-relation keyword priority. EODHD's ``ownerRelationship`` is
# free-form text mixing role and rank ("Chief Executive Officer", "Director",
# "Officer, Director", "10% Owner", combined commas, …). We keyword-match
# in priority order so one rule covers all the variants we've seen, then
# combine officer+director into the dual literal. Any non-empty string that
# matches none of these falls back to ``"other"`` so the column stays
# typed; ``None`` means the vendor didn't supply a value at all.
_OWNER_RELATION_TEN_PERCENT = ("10%", "ten percent")
_OWNER_RELATION_DIRECTOR_KW = "director"
_OWNER_RELATION_OFFICER_KW = "officer"


def _normalize_owner_relation(
    raw: Any,
) -> Literal["officer", "director", "officer_and_director", "ten_percent_owner", "other"] | None:
    if not isinstance(raw, str):
        return None
    text = raw.strip().lower()
    if not text:
        return None
    if any(needle in text for needle in _OWNER_RELATION_TEN_PERCENT):
        return "ten_percent_owner"
    has_officer = _OWNER_RELATION_OFFICER_KW in text
    has_director = _OWNER_RELATION_DIRECTOR_KW in text
    if has_officer and has_director:
        return "officer_and_director"
    if has_officer:
        return "officer"
    if has_director:
        return "director"
    return "other"


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


class EodhdFreeTierError(DataSourceError):
    """Raised when the API signals an endpoint is blocked on the free tier."""


class EodhdAllEndpointsFailedError(DataSourceError):
    """Raised when every parallel sub-fetch in ``fetch_metadata`` failed
    with a transport-class error (network, HTTP, JSON decode). Free-tier
    blocks do **not** trigger this — they're a legitimate steady state for
    free-tier users and produce empty bundles instead. The pipeline's
    per-ticker soft-fail catches this so the run continues with the
    failed ticker recorded in ``tickers_failed``."""


class EodhdFieldParseError(DataSourceError):
    """Raised when a vendor field fails Pydantic validation on the way
    into one of our row schemas (e.g. ``CouponRate=\"approx 4%\"``,
    ``CirculatingSupply=-1``). Wraps the ``ValidationError`` with vendor
    + ticker context so the operator-facing log line names the data
    quality issue, not the schema constraint."""


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
        return
    kept = 0
    dropped = 0
    try:
        for row in payload:
            if not isinstance(row, dict):
                dropped += 1
                continue
            ts = row.get("timestamp")
            if ts is None:
                dropped += 1
                continue
            try:
                ts_int = int(ts)
            except (TypeError, ValueError):
                dropped += 1
                continue
            when = datetime.fromtimestamp(ts_int, tz=UTC)
            close_val = row.get("close")
            if close_val is None:
                dropped += 1
                continue
            kept += 1
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
    finally:
        _log_parse_drops("parse_intraday_response", ticker, kept, dropped)


def parse_financial_statements_response(ticker: str, payload: Any) -> FinancialStatementsBundle:
    """Parse the EODHD ``/fundamentals`` payload into the three statement
    streams.

    EODHD returns all three financial statements under one ``Financials``
    key; we project each section into its statement-specific row type so
    downstream code never has to know vendor camelCase names.
    """
    _check_free_tier(payload)
    if not isinstance(payload, dict):
        return FinancialStatementsBundle()
    financials = payload.get("Financials") or {}
    return FinancialStatementsBundle(
        income=tuple(
            _iter_statement_section(
                ticker,
                financials.get("Income_Statement") or {},
                _INCOME_LINE_ITEMS,
                IncomeStatementRow,
            )
        ),
        balance=tuple(
            _iter_statement_section(
                ticker,
                financials.get("Balance_Sheet") or {},
                _BALANCE_LINE_ITEMS,
                BalanceSheetRow,
            )
        ),
        cashflow=tuple(
            _iter_statement_section(
                ticker,
                financials.get("Cash_Flow") or {},
                _CASHFLOW_LINE_ITEMS,
                CashFlowStatementRow,
            )
        ),
    )


def _iter_statement_section(
    ticker: str,
    section: dict,
    line_item_map: dict[str, str],
    row_cls: type,
) -> Iterator[Any]:
    """Yield ``row_cls`` instances from one statement section.

    ``section`` is the vendor object under ``Financials.<Statement_Name>``;
    inside it sit ``quarterly`` and ``yearly`` sub-objects keyed by
    period date strings. We project each period dict into one wide row
    by translating known camelCase keys via ``line_item_map`` and dropping
    everything else.

    Unknown frequency sub-keys (e.g. a future ``ttm`` block) are logged
    once at info-level so a vendor extension doesn't silently leak
    data; malformed period entries are counted and reported via the
    shared ``_log_parse_drops`` channel so silent corruption surfaces
    in observability.
    """
    statement_label = row_cls.__name__
    if isinstance(section, dict):
        for sub_key in section:
            if sub_key not in _FREQUENCY_MAP and sub_key != "currency_symbol":
                _log_unknown_vendor_value(
                    "eodhd.statement.unknown_frequency",
                    {"statement": statement_label, "ticker": ticker, "key": sub_key},
                )
    kept = 0
    dropped = 0
    try:
        for vendor_freq, canonical_freq in _FREQUENCY_MAP.items():
            periods = section.get(vendor_freq) or {}
            if not isinstance(periods, dict):
                continue
            for period_key, period_dict in periods.items():
                if not isinstance(period_dict, dict):
                    dropped += 1
                    continue
                period_end = _parse_date(period_dict.get("date") or period_key)
                if period_end is None:
                    dropped += 1
                    continue
                kwargs: dict[str, Any] = {
                    "ticker": ticker,
                    "period_end": period_end,
                    "frequency": canonical_freq,
                    "filing_date": _parse_date(period_dict.get("filing_date")),
                    "currency": period_dict.get("currency_symbol"),
                }
                # ``vendor_key in period_dict`` collapses "absent" and
                # "explicit null" to the same write-NULL semantic. The
                # lake's statement upsert uses COALESCE(EXCLUDED, table)
                # in the UPDATE clause so NULL doesn't overwrite a prior
                # non-NULL value — but it also means a vendor explicitly
                # restating a line to NULL is a no-op on update. That's
                # the right trade-off here (vendor flake > silent wipe).
                for vendor_key, canonical_key in line_item_map.items():
                    if vendor_key in period_dict:
                        kwargs[canonical_key] = _coerce_optional_float(period_dict[vendor_key])
                kept += 1
                yield row_cls(**kwargs)
    finally:
        _log_parse_drops(f"_iter_statement_section[{statement_label}]", ticker, kept, dropped)


def _check_free_tier(payload: Any) -> None:
    if isinstance(payload, str):
        lowered = payload.lower()
        if any(marker in lowered for marker in _FREE_TIER_MARKERS):
            raise EodhdFreeTierError(payload.strip())


# Pure parsers don't have a bound source-instance logger, so they share a
# module-level structlog logger for emitting drop-rate observability when
# they have to skip malformed vendor rows.
_PARSER_LOG = get_logger("stonks.ingest.sources.eodhd.parsers")


def _log_unknown_vendor_value(event: str, raw: Any) -> None:
    """Emit a single info-level event when a normalizer encounters a
    vendor string outside its known keyword set. Distinct from "vendor
    said nothing" (the field is empty / non-string) — this is "vendor
    said *something* and we couldn't classify it", which is the signal
    we extend the keyword table from.
    """
    _PARSER_LOG.info(event, raw=raw)


# Threshold above which a "we dropped some rows" event is loud (WARN
# = canary for vendor schema break) rather than quiet (DEBUG).
_HIGH_DROP_RATIO = 0.5


def _log_parse_drops(parser: str, ticker: str, kept: int, dropped: int) -> None:
    """Emit a structured log line summarizing how many rows a parser had to
    skip. Quiet at DEBUG for the occasional bad row; LOUD at WARN when the
    drop ratio crosses ``_HIGH_DROP_RATIO`` (a strong signal that the vendor
    changed its response shape and we're silently losing data)."""
    if dropped == 0:
        return
    total = kept + dropped
    drop_rate = dropped / total if total > 0 else 0.0
    if drop_rate >= _HIGH_DROP_RATIO:
        _PARSER_LOG.warning(
            "eodhd.parser.high_drop_rate",
            parser=parser,
            ticker=ticker,
            kept=kept,
            dropped=dropped,
            drop_rate=round(drop_rate, 3),
        )
    else:
        _PARSER_LOG.debug(
            "eodhd.parser.drops",
            parser=parser,
            ticker=ticker,
            kept=kept,
            dropped=dropped,
        )


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
            involvement_raw = entry.get("Involvement")
            if not activity or involvement_raw is None:
                continue
            canonical_involvement = _INVOLVEMENT_MAP.get(str(involvement_raw).strip().lower())
            if canonical_involvement is None:
                # Unknown vendor value (e.g. "Maybe", localized text). Drop
                # the row rather than store an unnormalized string — keeps
                # the schema's Literal["yes","no"] honest.
                continue
            activities.append(
                EsgActivityRow(
                    ticker=ticker,
                    rating_date=rating_date,
                    activity=str(activity),
                    involvement=canonical_involvement,
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
        return
    kept = 0
    dropped = 0
    try:
        for row in payload:
            if not isinstance(row, dict):
                dropped += 1
                continue
            ex_date = _parse_date(row.get("date"))
            amount = _coerce_optional_float(row.get("value"))
            if ex_date is None or amount is None or amount < 0:
                dropped += 1
                continue
            kept += 1
            yield DividendRow(
                ticker=ticker,
                ex_date=ex_date,
                amount=amount,
                currency=row.get("currency"),
                pay_date=_parse_date(row.get("paymentDate")),
                record_date=_parse_date(row.get("recordDate")),
                declaration_date=_parse_date(row.get("declarationDate")),
            )
    finally:
        _log_parse_drops("parse_dividends_response", ticker, kept, dropped)


def parse_news_response(ticker: str, payload: Any) -> Iterator[NewsArticleRow]:
    _check_free_tier(payload)
    if not isinstance(payload, list):
        return
    kept = 0
    dropped = 0
    try:
        for row in payload:
            if not isinstance(row, dict):
                dropped += 1
                continue
            published_at = _parse_datetime(row.get("date"))
            title = row.get("title")
            if published_at is None or not title:
                dropped += 1
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
            kept += 1
            yield NewsArticleRow(
                ticker=ticker,
                published_at=published_at,
                title=title,
                url=row.get("link"),
                source_name=row.get("source") or row.get("source_name"),
                # `content` is deliberately not populated: storing every
                # article body would inflate the lake without a current
                # consumer. The schema column exists so a future flip can
                # begin populating it without a migration.
                content=None,
                symbols=_str_tuple(row.get("symbols")),
                tags=_str_tuple(row.get("tags")),
                sentiment=sentiment_polarity,
                sentiment_pos=sentiment_pos,
                sentiment_neg=sentiment_neg,
                sentiment_neu=sentiment_neu,
            )
    finally:
        _log_parse_drops("parse_news_response", ticker, kept, dropped)


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
            owner_relation=_normalize_owner_relation(row.get("ownerRelationship")),
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
        return
    kept = 0
    dropped = 0
    try:
        for row in payload:
            if not isinstance(row, dict):
                dropped += 1
                continue
            d = _parse_date(row.get("date"))
            split_str = row.get("split")
            if d is None or not isinstance(split_str, str) or "/" not in split_str:
                dropped += 1
                continue
            try:
                new, old = split_str.split("/", 1)
                ratio = float(new) / float(old)
            except (ValueError, ZeroDivisionError):
                dropped += 1
                continue
            if ratio <= 0:
                dropped += 1
                continue
            kept += 1
            yield StockSplitRow(ticker=ticker, date=d, ratio=ratio)
    finally:
        _log_parse_drops("parse_splits_response", ticker, kept, dropped)


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


# ---- multi-asset classification + parsers ---------------------------------

# Closed map: ticker suffix → AssetClass. Keys are upper-cased EODHD
# virtual exchanges; .US / .LSE / .XETRA / etc. fall through to "equity".
_ASSET_CLASS_SUFFIX_MAP: dict[str, AssetClass] = {
    "CC": "crypto",
    "COMM": "commodity",
    "GBOND": "bond",
}


def classify_asset_class(ticker: str) -> AssetClass:
    """Suffix-based AssetClass classifier for EODHD ticker strings.

    EODHD encodes the asset class in the ticker suffix: ``BTC-USD.CC``,
    ``GC.COMM``, ``US10Y.GBOND``. Equities use the exchange code
    (``AAPL.US``, ``VOD.LSE``); the equity case is open-ended so we
    treat it as the default — anything not in the explicit non-equity
    map falls through to ``"equity"``, which matches the migration 007
    backfill of pre-existing rows.
    """
    if "." not in ticker:
        return "equity"
    suffix = ticker.rsplit(".", 1)[1].upper()
    return _ASSET_CLASS_SUFFIX_MAP.get(suffix, "equity")


_CRYPTO_CONSENSUS_KEYWORDS: tuple[tuple[str, CryptoConsensusType], ...] = (
    # Order matters — broader / abbreviated terms after the more specific
    # ones so e.g. "delegated" beats "stake" and "dpos" beats "pos".
    ("delegated", "delegated_proof_of_stake"),
    ("dpos", "delegated_proof_of_stake"),
    ("stake", "proof_of_stake"),
    ("pos", "proof_of_stake"),
    ("work", "proof_of_work"),
    ("pow", "proof_of_work"),
    # Vendors do explicitly say "Other" — pass it through rather than
    # collapsing into the "unknown vocabulary" bucket below.
    ("other", "other"),
)


def _normalize_consensus_type(raw: Any) -> CryptoConsensusType | None:
    """Map EODHD's free-form ``ConsensusType`` strings ('Proof-of-Work',
    'Proof of Stake', 'PoS', 'DPoS', …) into the schema's closed Literal.

    Returns ``None`` for unknown vendor strings (and logs them, so the
    keyword table can be extended when EODHD coins something new) —
    that's distinct from the explicit ``"other"`` literal, which we
    emit when the vendor *said* "Other".
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip().lower()
    if not text:
        return None
    for needle, value in _CRYPTO_CONSENSUS_KEYWORDS:
        if needle in text:
            return value
    _log_unknown_vendor_value("eodhd.normalize.unknown_consensus_type", raw)
    return None


def _split_crypto_pair(general: dict, ticker: str) -> tuple[str | None, str | None]:
    """Derive base / quote symbols for a crypto pair.

    EODHD's crypto ``General.Code`` is the *full* pair (``"BTC-USD"``)
    and ``CurrencyCode`` is the quote (``"USD"``). The base symbol
    isn't exposed as its own field, so we always derive it from the
    ticker prefix when it carries a ``-`` separator. ``CurrencyCode``
    wins for quote when present; otherwise we fall back to the second
    half of the prefix.
    """
    quote = general.get("CurrencyCode")
    base: str | None = None
    prefix = ticker.rsplit(".", 1)[0]
    if "-" in prefix:
        base_default, quote_default = prefix.split("-", 1)
        base = base_default
        quote = quote or quote_default
    return base, quote


def _build_row_or_field_error(row_cls, ticker: str, /, **kwargs: Any) -> Any:
    """Construct a Pydantic row, translating any ``ValidationError`` into
    an :class:`EodhdFieldParseError` that names the vendor + ticker
    first. The pipeline's per-ticker soft-fail catches ``DataSourceError``,
    so the run continues with this ticker recorded as failed — but the
    error message reads like a vendor data-quality issue, not a schema
    bug.
    """
    try:
        return row_cls(**kwargs)
    except ValidationError as exc:
        raise EodhdFieldParseError(
            f"eodhd {row_cls.__name__} validation failed for {ticker}: {exc}"
        ) from exc


def parse_crypto_profile_response(ticker: str, payload: Any) -> CryptoProfileRow | None:
    """Build a :class:`CryptoProfileRow` from EODHD's crypto fundamentals
    payload.

    Returns ``None`` for non-dict payloads (free-tier blocks, transport
    errors), so the caller can no-op without a special case. When EODHD
    returns the dict but lacks the supply / blockchain block, the row
    still carries the ticker (and base/quote derived from the symbol)
    so identity is captured.

    Vendor data-quality failures (negative supply, supply chain
    inversion, …) raise :class:`EodhdFieldParseError` with the ticker
    named up front so the pipeline's failure log is actionable.
    """
    if not isinstance(payload, dict):
        return None
    general = payload.get("General") or {}
    components = payload.get("Components") or {}
    base, quote = _split_crypto_pair(general if isinstance(general, dict) else {}, ticker)
    components_dict = components if isinstance(components, dict) else {}
    return _build_row_or_field_error(
        CryptoProfileRow,
        ticker,
        ticker=ticker,
        base_symbol=base,
        quote_symbol=quote,
        blockchain=components_dict.get("Blockchain"),
        consensus_type=_normalize_consensus_type(components_dict.get("ConsensusType")),
        circulating_supply=components_dict.get("CirculatingSupply"),
        total_supply=components_dict.get("TotalSupply"),
        max_supply=components_dict.get("MaxSupply"),
        supply_snapshot_date=None,
    )


_BOND_ISSUER_KEYWORDS: tuple[tuple[str, BondIssuerKind], ...] = (
    ("sovereign", "sovereign"),
    ("government", "sovereign"),
    ("treasury", "sovereign"),
    ("supranational", "supranational"),
    ("agency", "agency"),
    ("municipal", "municipal"),
    ("muni", "municipal"),
    ("corporate", "corporate"),
    ("corp", "corporate"),
    # Pass through an explicit "Other" from the vendor; unknown strings
    # below fall through to ``None`` + a log line.
    ("other", "other"),
)
_BOND_KIND_KEYWORDS: tuple[tuple[str, BondKind], ...] = (
    ("treasury", "treasury"),
    ("zero", "zero_coupon"),
    ("municipal", "municipal"),
    ("muni", "municipal"),
    ("corporate", "corporate"),
    ("corp", "corporate"),
    ("other", "other"),
)


def _normalize_issuer_kind(raw: Any) -> BondIssuerKind | None:
    if not isinstance(raw, str):
        return None
    text = raw.strip().lower()
    if not text:
        return None
    for needle, value in _BOND_ISSUER_KEYWORDS:
        if needle in text:
            return value
    _log_unknown_vendor_value("eodhd.normalize.unknown_issuer_kind", raw)
    return None


def _normalize_bond_kind(raw: Any) -> BondKind | None:
    if not isinstance(raw, str):
        return None
    text = raw.strip().lower()
    if not text:
        return None
    for needle, value in _BOND_KIND_KEYWORDS:
        if needle in text:
            return value
    _log_unknown_vendor_value("eodhd.normalize.unknown_bond_kind", raw)
    return None


def _parse_iso_date(raw: Any) -> date | None:
    if not isinstance(raw, str):
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def parse_bond_profile_response(ticker: str, payload: Any) -> BondProfileRow | None:
    """Build a :class:`BondProfileRow` from EODHD's bond fundamentals
    payload (under the ``BondData`` key, when present).

    Returns ``None`` for non-dict payloads. Tickers EODHD has registered
    but exposes no bond-specific data for produce a ticker-only row so
    the instrument exists in our profile surface.
    """
    if not isinstance(payload, dict):
        return None
    bond_data = payload.get("BondData") or {}
    general = payload.get("General") or {}
    if not isinstance(bond_data, dict):
        bond_data = {}
    if not isinstance(general, dict):
        general = {}
    return _build_row_or_field_error(
        BondProfileRow,
        ticker,
        ticker=ticker,
        issuer_name=bond_data.get("Issuer"),
        issuer_kind=_normalize_issuer_kind(bond_data.get("IssuerType")),
        bond_kind=_normalize_bond_kind(bond_data.get("BondType")),
        coupon_rate=bond_data.get("CouponRate"),
        coupon_frequency=bond_data.get("CouponFrequency"),
        face_value=bond_data.get("FaceValue"),
        currency=bond_data.get("Currency") or general.get("CurrencyCode"),
        issue_date=_parse_iso_date(bond_data.get("IssueDate")),
        maturity_date=_parse_iso_date(bond_data.get("MaturityDate")),
        credit_rating=bond_data.get("CreditRating"),
    )


_COMMODITY_KIND_KEYWORDS: tuple[tuple[str, CommodityContractKind], ...] = (
    ("continuous", "continuous"),
    ("futures", "futures"),
    ("future", "futures"),
    ("spot", "spot"),
    ("index", "index"),
    ("other", "other"),
)


def _normalize_contract_kind(raw: Any) -> CommodityContractKind | None:
    if not isinstance(raw, str):
        return None
    text = raw.strip().lower()
    if not text:
        return None
    for needle, value in _COMMODITY_KIND_KEYWORDS:
        if needle in text:
            return value
    _log_unknown_vendor_value("eodhd.normalize.unknown_contract_kind", raw)
    return None


def parse_commodity_contract_response(ticker: str, payload: Any) -> CommodityContractRow | None:
    """Build a :class:`CommodityContractRow` from EODHD's commodity
    fundamentals payload (under the ``ContractData`` key, when present).

    Returns ``None`` for non-dict payloads. Tickers without contract
    metadata produce a ticker-only row.
    """
    if not isinstance(payload, dict):
        return None
    contract_data = payload.get("ContractData") or {}
    if not isinstance(contract_data, dict):
        contract_data = {}
    return _build_row_or_field_error(
        CommodityContractRow,
        ticker,
        ticker=ticker,
        underlying_symbol=contract_data.get("UnderlyingSymbol"),
        contract_kind=_normalize_contract_kind(contract_data.get("ContractType")),
        contract_month=contract_data.get("ContractMonth"),
        expiry_date=_parse_iso_date(contract_data.get("ExpiryDate")),
        contract_size=contract_data.get("ContractSize"),
        contract_unit=contract_data.get("ContractUnit"),
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
        # If either response isn't the list we expect, log loudly *before*
        # falling through. A silent skip here would re-introduce survivorship
        # bias on the delisted side without anyone noticing.
        for label, data in (("active", active), ("delisted", delisted)):
            if not isinstance(data, list):
                self._log.warning(
                    "eodhd.list_tickers.unexpected_payload_shape",
                    exchange=exchange,
                    leg=label,
                    payload_type=type(data).__name__,
                    note=(
                        "expected a JSON list; "
                        + (
                            "delisted leg failed → returned universe is survivorship-biased"
                            if label == "delisted"
                            else "active leg failed → returned universe is incomplete"
                        )
                    ),
                )
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

    def fetch_fundamentals(self, ticker: str) -> FinancialStatementsBundle:
        # Income/balance/cashflow statements are equity-only. Crypto/bond/
        # commodity tickers don't have an issuer with financial statements,
        # so we short-circuit instead of issuing an HTTP call that would
        # come back empty (or, on EODHD, with a free-tier text error).
        asset_class = classify_asset_class(ticker)
        if asset_class != "equity":
            self._log.info(
                "eodhd.fundamentals.skipped_non_equity",
                ticker=ticker,
                asset_class=asset_class,
            )
            return FinancialStatementsBundle()
        url = f"{self._base_url}/fundamentals/{ticker}"
        data = self._get(url, params={"fmt": "json"})
        return parse_financial_statements_response(ticker, data)

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

    def fetch_metadata(self, ticker: str) -> MetadataBundle:
        """Assemble the full metadata bundle by hitting seven EODHD endpoints
        in parallel (``requests.Session`` is thread-safe for concurrent GETs).

        Each sub-fetch is wrapped in free-tier / network error tolerance:
        a subscription-blocked or transport error on one endpoint leaves the
        corresponding bundle field empty; other fields still populate. If
        *every* endpoint fails with a transport-class error (network/JSON
        decode), the whole fetch raises ``EodhdAllEndpointsFailedError``
        rather than returning an empty bundle that looks like a successful
        free-tier user — that's the difference between "vendor said no
        data" and "we couldn't reach the vendor at all."

        The single ``/fundamentals`` payload contributes the lion's share —
        profile, ticker_snapshot, holders, earnings_announcements,
        analyst_forecasts, analyst_ratings, esg_snapshot/activities,
        cross_listings, officers, shares_outstanding history, employee_count.

        Non-equity tickers (crypto/bond/commodity) take a narrower path:
        only the ``/fundamentals`` endpoint is queried (it returns the
        per-class profile fields we model), and the equity-shaped sub-fetches
        (dividends, splits, market_cap, news, insider, sentiments) are
        skipped — they don't apply.
        """
        if classify_asset_class(ticker) != "equity":
            return self._fetch_metadata_non_equity(ticker)
        jobs = {
            "fundamentals": lambda: self._get_json(f"/fundamentals/{ticker}"),
            "dividends": lambda: self._get_json(f"/div/{ticker}", {"fmt": "json"}),
            "splits": lambda: self._get_json(f"/splits/{ticker}", {"fmt": "json"}),
            "market_cap": lambda: self._get_json(
                f"/historical-market-cap/{ticker}",
                {"fmt": "json"},
            ),
            "news": lambda: self._get_json("/news", {"fmt": "json", "s": ticker}),
            "insider": lambda: self._get_json(
                "/insider-transactions",
                {"fmt": "json", "code": ticker},
            ),
            "sentiments": lambda: self._get_json("/sentiments", {"fmt": "json", "s": ticker}),
        }

        # Concurrent dict writes from worker threads — Python's GIL makes
        # ``dict[str] = str`` atomic, and each worker writes a unique key,
        # so no extra synchronization is needed.
        errors: dict[str, str] = {}
        with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
            futures = {
                name: pool.submit(self._try, fn, endpoint=name, ticker=ticker, errors=errors)
                for name, fn in jobs.items()
            }
            results = {name: fut.result() for name, fut in futures.items()}

        transport_failures = sum(1 for kind in errors.values() if kind == "transport")
        if transport_failures == len(jobs):
            # Every endpoint failed with a network/HTTP/JSON error — that's
            # a real outage, not a steady-state empty bundle. Surface it so
            # the pipeline can record this ticker as failed.
            raise EodhdAllEndpointsFailedError(
                f"all {len(jobs)} metadata endpoints failed for {ticker} with transport errors"
            )

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

    def _fetch_metadata_non_equity(self, ticker: str) -> MetadataBundle:
        """Narrow metadata fetch for crypto / bond / commodity tickers.

        Only the ``/fundamentals`` endpoint is queried — it returns the
        per-class profile fields we model. Equity-shaped sub-fetches
        (dividends, splits, market cap history, insider transactions,
        analyst data, news, sentiments) are skipped because they don't
        apply to these classes; the source-of-truth principle is that
        the bundle field for an inapplicable surface stays at its
        default (``None`` / ``()``).

        Error semantics mirror the equity path's ``_try`` accounting:
        an ``EodhdFreeTierError`` on the sole endpoint is the steady
        state for free-tier users and produces an instrument-only
        profile row. Transport / JSON failures, however, are an outage
        — there's only one endpoint, so a transport failure here is
        the equivalent of "all endpoints failed" in the equity path.
        We surface those as ``EodhdAllEndpointsFailedError`` so the
        pipeline records the ticker in ``tickers_failed`` instead of
        silently incrementing ``tickers_ok`` with an empty profile.
        """
        asset_class = classify_asset_class(ticker)
        errors: dict[str, str] = {}
        payload = self._try(
            lambda: self._get_json(f"/fundamentals/{ticker}"),
            endpoint="fundamentals",
            ticker=ticker,
            errors=errors,
        )
        if errors.get("fundamentals") == "transport":
            # Sole endpoint failed with a transport-class error — that's
            # an outage, not a steady-state empty bundle. Equity path
            # raises this when *all* endpoints fail; with a single
            # endpoint here, "all" and "the one" coincide.
            raise EodhdAllEndpointsFailedError(
                f"sole metadata endpoint /fundamentals failed for {ticker} "
                f"(non-equity, asset_class={asset_class})"
            )

        bundle_parts: dict[str, Any] = {
            "profile": TickerProfile(id=ticker, asset_class=asset_class),
        }
        if payload is None:
            return MetadataBundle(**bundle_parts)

        if asset_class == "crypto":
            row = parse_crypto_profile_response(ticker, payload)
            if row is not None:
                bundle_parts["crypto_profile"] = row
        elif asset_class == "bond":
            row = parse_bond_profile_response(ticker, payload)
            if row is not None:
                bundle_parts["bond_profile"] = row
        elif asset_class == "commodity":
            row = parse_commodity_contract_response(ticker, payload)
            if row is not None:
                bundle_parts["commodity_contract"] = row

        # Re-use the equity profile parser to enrich the instrument row
        # with whatever shared static fields EODHD happens to expose
        # (Name, CurrencyCode, Description). ``security_type`` is force-
        # cleared to None: the spec keeps it equity-only, and EODHD's
        # ``Type`` field is not orthogonal to ``asset_class`` — it
        # returns "Currency" / "FUND" / etc. for non-equity rows that
        # would otherwise leak into the equity sub-kind column.
        equity_profile = parse_profile_from_fundamentals(ticker, payload)
        if equity_profile is not None:
            bundle_parts["profile"] = equity_profile.model_copy(
                update={"asset_class": asset_class, "security_type": None}
            )

        return MetadataBundle(**bundle_parts)

    def _get_json(self, path: str, params: dict[str, str] | None = None) -> Any:
        return self._get(f"{self._base_url}{path}", params=params or {"fmt": "json"})

    def _try(self, fn, *, endpoint: str, ticker: str, errors: dict[str, str]):
        """Run ``fn`` and swallow *known-safe* error types so one blocked
        endpoint doesn't poison the rest of the metadata bundle. Only
        ``EodhdFreeTierError`` (subscription), ``requests.RequestException``
        (transport), and JSON decode errors are suppressed — everything
        else (ValidationError, KeyError, AttributeError, TypeError) is a
        programming bug and propagates so tests can catch it.

        Records the failure kind in ``errors[endpoint]`` so the caller can
        distinguish "ticker had nothing to report on the free tier" from
        "we couldn't reach the vendor" — both produce ``None`` here, but
        only the latter should escalate to a full-fetch failure.
        """
        try:
            return fn()
        except EodhdFreeTierError as exc:
            errors[endpoint] = "free_tier"
            self._log.info(
                "eodhd.metadata.skipped_free_tier",
                ticker=ticker,
                endpoint=endpoint,
                reason=str(exc),
            )
            return None
        except (requests.RequestException, json.JSONDecodeError) as exc:
            errors[endpoint] = "transport"
            self._log.warning(
                "eodhd.metadata.skipped_error",
                ticker=ticker,
                endpoint=endpoint,
                error=f"{type(exc).__name__}: {exc}",
            )
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
                    lowered = text.lower()
                    if any(marker in lowered for marker in _FREE_TIER_MARKERS):
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

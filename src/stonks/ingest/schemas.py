"""Canonical ingestion row schemas.

These are the *normalized*, vendor-agnostic rows that flow from any
``DataSource`` into the lake — vendor-specific shapes are translated up-front,
so the pipeline and the lake never see vendor fields.

Schemas cover the full fundamentals surface area (the three financial
statements — income, balance sheet, cash flow — plus dividends, insider
trades, news, analyst data, shares outstanding, employee count,
segmentations, profile, holders, earnings events, ESG, listings, officers)
so strategies can draw on whatever they need without each layer having to
be extended.

The three financial statements each have their own row type
(:class:`IncomeStatementRow`, :class:`BalanceSheetRow`,
:class:`CashFlowStatementRow`) with a snake_case column per known line
item. Every statement-specific value is ``Optional[float]`` because vendors
routinely omit lines that don't apply to a given filer (e.g. banks have no
``cost_of_revenue``, software firms have no ``inventory``).

Vendor-agnostic principle (CLAUDE.md): fields whose vocabulary varies across
vendors use ``Literal`` values that adapters map their raw strings into;
fields with no cross-vendor analogue are not modelled here.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from stonks.core.types import AssetClass

# Closed Literal aliases for the per-asset-class profile fields.
# Exposed at module scope so adapters import the canonical set instead
# of redeclaring their own ``Literal[...]`` for normalizer return types.
CryptoConsensusType = Literal[
    "proof_of_work", "proof_of_stake", "delegated_proof_of_stake", "other"
]
BondIssuerKind = Literal["sovereign", "corporate", "municipal", "agency", "supranational", "other"]
BondKind = Literal["treasury", "corporate", "municipal", "zero_coupon", "other"]
CommodityContractKind = Literal["spot", "continuous", "futures", "index", "other"]
#: Equity sub-kind shared by :class:`TickerProfile` and :class:`SymbolListing`.
SecurityType = Literal["common_stock", "preferred_stock", "adr", "etf", "fund", "other"]

# Re-export for DataSource implementations that need to import from the
# schemas module in a single statement.
__all__ = [
    "AnalystForecastRow",
    "AnalystRatingsRow",
    "BalanceSheetRow",
    "BondIssuerKind",
    "BondKind",
    "BondProfileRow",
    "BondYieldRow",
    "CashFlowStatementRow",
    "CommodityContractKind",
    "CommodityContractRow",
    "CrossListingRow",
    "CryptoConsensusType",
    "CryptoProfileRow",
    "DefiTvlRow",
    "DividendRow",
    "EarningsAnnouncementRow",
    "EmployeeCountRow",
    "EsgActivityRow",
    "EsgSnapshotRow",
    "ExchangeInfo",
    "FinancialStatementsBundle",
    "IncomeStatementRow",
    "InsiderTransactionRow",
    "InstitutionalHolderRow",
    "IntradayBar",
    "MacroIndicatorRow",
    "MacroPeriod",
    "MarketCapRow",
    "NewsArticleRow",
    "NewsSentimentRow",
    "OfficerRow",
    "RawPriceBar",
    "SecurityType",
    "SymbolListing",
    "SegmentationRow",
    "SharesOutstandingRow",
    "StockSplitRow",
    "StatementFrequency",
    "TickerProfile",
    "TickerSnapshotRow",
]


# ``Q`` = quarterly, ``A`` = annual. Same alias used by every statement row
# so a downstream filter or join can stay literal-typed.
StatementFrequency = Literal["Q", "A"]


class FrozenRow(BaseModel):
    """Shared base for all immutable schema rows.

    Centralises ``frozen=True`` so every row class is hashable and can't be
    mutated after construction — see CLAUDE.md's "vendor-agnostic schemas"
    rule for why we treat normalised rows as values, not records.
    """

    model_config = ConfigDict(frozen=True)


# ---- prices + financial statements (original surface) ----------------------


class RawPriceBar(FrozenRow):
    """One daily bar as a vendor sent it. Prices may be missing (``None``):
    the ingest quality checker quarantines such a row as ``missing_price``,
    so one bad vendor row never fails the ticker's whole fetch."""

    ticker: str
    date: date
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    adj_close: float | None
    volume: int | None = None


class IntradayBar(FrozenRow):
    """Sub-daily bar — same shape as :class:`RawPriceBar` but with a full
    ``datetime`` instead of a plain date. Flows directly into the ``bars``
    table via ``DuckDBLake.upsert_bars`` with an explicit ``interval``."""

    ticker: str
    timestamp: datetime
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    adj_close: float | None
    volume: int | None = None


class _StatementRowBase(FrozenRow):
    """Shared key columns for the three financial-statement rows.

    Each statement subclass adds its own wide set of statement-specific
    columns; this base centralises the ``(ticker, period_end,
    frequency)`` primary key and the two header columns
    (``filing_date``, ``currency``) every statement carries.
    """

    ticker: str
    period_end: date
    frequency: StatementFrequency
    filing_date: date | None = None
    # Reporting currency for the statement (typically a 3-letter ISO-4217
    # code, but vendors don't all enforce that — EODHD's
    # ``currency_symbol`` is whatever the issuer files in). Stored as
    # free-text rather than ``Literal[...]`` because the realistic set
    # spans 100+ currencies and any closed list would lock out exotic
    # filers; downstream consumers should uppercase + length-check
    # before equality comparisons.
    currency: str | None = None


class IncomeStatementRow(_StatementRowBase):
    """One income-statement filing for a (ticker, period_end, frequency).

    Every value is ``Optional[float]``: vendors omit lines that don't
    apply to a given filer (e.g. banks have no ``cost_of_revenue``).
    Names are the canonical accounting concept in snake_case — the
    vendor camelCase (``totalRevenue``, ``grossProfit``, …) is mapped
    by the adapter so the lake never sees vendor vocabulary.
    """

    revenue: float | None = None
    cost_of_revenue: float | None = None
    gross_profit: float | None = None

    research_development: float | None = None
    selling_general_administrative: float | None = None
    selling_marketing_expenses: float | None = None
    other_operating_expenses: float | None = None
    total_operating_expenses: float | None = None

    operating_income: float | None = None

    interest_income: float | None = None
    interest_expense: float | None = None
    net_interest_income: float | None = None
    non_operating_income_other: float | None = None
    total_other_income_expense_net: float | None = None

    income_before_tax: float | None = None
    income_tax_expense: float | None = None
    tax_provision: float | None = None

    minority_interest: float | None = None
    net_income_continuing: float | None = None
    discontinued_operations: float | None = None
    extraordinary_items: float | None = None
    non_recurring: float | None = None
    other_items: float | None = None
    effect_of_accounting_charges: float | None = None
    net_income: float | None = None
    net_income_to_common: float | None = None
    preferred_stock_adjustments: float | None = None

    # Vendor-supplied EBIT/EBITDA: kept as columns rather than re-derived
    # downstream because vendors apply their own one-time-item exclusions.
    ebit: float | None = None
    ebitda: float | None = None
    depreciation_amortization: float | None = None
    reconciled_depreciation: float | None = None


class BalanceSheetRow(_StatementRowBase):
    """One balance-sheet filing for a (ticker, period_end, frequency).

    Wide schema (one column per known line item). Asset / liability /
    equity lines are grouped by section in the column order so a SELECT *
    reads top-to-bottom like the statement itself.
    """

    # Asset side
    total_assets: float | None = None
    current_assets: float | None = None
    cash: float | None = None
    cash_and_equivalents: float | None = None
    cash_and_short_term_investments: float | None = None
    short_term_investments: float | None = None
    net_receivables: float | None = None
    inventory: float | None = None
    other_current_assets: float | None = None

    non_current_assets: float | None = None
    long_term_investments: float | None = None
    property_plant_equipment_net: float | None = None
    property_plant_equipment_gross: float | None = None
    accumulated_depreciation: float | None = None
    accumulated_amortization: float | None = None
    goodwill: float | None = None
    intangible_assets: float | None = None
    other_assets: float | None = None
    deferred_long_term_asset_charges: float | None = None
    non_current_assets_other: float | None = None
    earning_assets: float | None = None

    # Liability side
    total_liabilities: float | None = None
    current_liabilities: float | None = None
    accounts_payable: float | None = None
    current_deferred_revenue: float | None = None
    short_term_debt: float | None = None
    short_long_term_debt: float | None = None
    short_long_term_debt_total: float | None = None
    other_current_liabilities: float | None = None

    non_current_liabilities: float | None = None
    long_term_debt: float | None = None
    long_term_debt_total: float | None = None
    capital_lease_obligations: float | None = None
    deferred_long_term_liabilities: float | None = None
    other_liabilities: float | None = None
    non_current_liabilities_other: float | None = None
    negative_goodwill: float | None = None
    warrants: float | None = None
    preferred_stock_redeemable: float | None = None

    # Equity
    total_stockholder_equity: float | None = None
    common_stock: float | None = None
    capital_stock: float | None = None
    additional_paid_in_capital: float | None = None
    retained_earnings: float | None = None
    treasury_stock: float | None = None
    accumulated_other_comprehensive_income: float | None = None
    other_stockholder_equity: float | None = None
    common_stock_total_equity: float | None = None
    preferred_stock_total_equity: float | None = None
    retained_earnings_total_equity: float | None = None
    capital_surplus: float | None = None
    total_permanent_equity: float | None = None
    noncontrolling_interest: float | None = None
    temporary_equity_redeemable_noncontrolling: float | None = None
    liabilities_and_stockholders_equity: float | None = None

    # Aggregates / vendor-derived
    net_debt: float | None = None
    net_tangible_assets: float | None = None
    net_working_capital: float | None = None
    investments: float | None = None
    common_stock_shares_outstanding: float | None = None


class CashFlowStatementRow(_StatementRowBase):
    """One cash-flow-statement filing for a (ticker, period_end, frequency).

    Sections (operating / investing / financing) are grouped in column
    order; the section subtotals (``operating_cash_flow``, etc.) sit at
    the top so they're discoverable without scanning the breakdown.
    """

    # Headline subtotals
    operating_cash_flow: float | None = None
    investing_cash_flow: float | None = None
    financing_cash_flow: float | None = None

    # Operating-section detail
    net_income: float | None = None
    depreciation: float | None = None
    stock_based_compensation: float | None = None
    change_in_working_capital: float | None = None
    change_to_inventory: float | None = None
    change_to_account_receivables: float | None = None
    change_to_liabilities: float | None = None
    change_to_operating_activities: float | None = None
    change_to_net_income: float | None = None
    change_receivables: float | None = None
    cash_flows_other_operating: float | None = None
    other_non_cash_items: float | None = None

    # Investing-section detail
    capital_expenditures: float | None = None
    investments: float | None = None
    other_cash_flows_investing: float | None = None

    # Financing-section detail
    dividends_paid: float | None = None
    net_borrowings: float | None = None
    issuance_of_capital_stock: float | None = None
    sale_purchase_of_stock: float | None = None
    other_cash_flows_financing: float | None = None

    # Period reconciliation
    change_in_cash: float | None = None
    cash_and_cash_equivalents_changes: float | None = None
    begin_period_cash_flow: float | None = None
    end_period_cash_flow: float | None = None
    exchange_rate_changes: float | None = None

    # Vendor-derived
    free_cash_flow: float | None = None


class FinancialStatementsBundle(FrozenRow):
    """Three parallel streams returned by :meth:`DataSource.fetch_fundamentals`.

    Vendors that expose all three statements in a single endpoint
    (EODHD's ``/fundamentals``) populate all three lists from one call;
    sources that only carry one of them leave the others empty.

    The bundle is the unit of "fundamentals for one ticker" — the
    ingest pipeline upserts the three lists inside one transaction so
    a partial failure doesn't leave the lake half-populated.
    """

    income: tuple[IncomeStatementRow, ...] = ()
    balance: tuple[BalanceSheetRow, ...] = ()
    cashflow: tuple[CashFlowStatementRow, ...] = ()


# ---- time-series metadata --------------------------------------------------


class DividendRow(FrozenRow):
    ticker: str
    ex_date: date
    #: Cash paid per share as the share existed on ``ex_date`` — never
    #: restated for later splits (adapters map the vendor's unadjusted
    #: figure here).
    amount: float = Field(ge=0.0)
    currency: str | None = None
    pay_date: date | None = None
    record_date: date | None = None
    declaration_date: date | None = None


class InsiderTransactionRow(FrozenRow):
    """One insider trade. Captures both the trade event and the SEC filing.

    ``transaction_date`` is the actual trade; ``filing_date`` is when the
    Form 4 was filed (typically a few days later). Both are useful: the lag
    itself is a signal, and event-study windows anchor on the trade date
    while disclosure-flow studies anchor on the filing date.
    """

    ticker: str
    transaction_date: date
    filing_date: date | None = None
    owner_name: str | None = None
    owner_cik: str | None = None
    # Normalized cross-vendor relation. Adapters (e.g. EODHD's
    # ``ownerRelationship`` keyword-match) map their raw vendor strings into
    # this closed set at parse time so strategies can group by it directly.
    owner_relation: (
        Literal["officer", "director", "officer_and_director", "ten_percent_owner", "other"] | None
    ) = None
    owner_title: str | None = None  # free text e.g. "CEO"
    transaction_code: str | None = None  # SEC Form 4 code (S, P, A, M, …)
    acquired_disposed: Literal["A", "D"] | None = None
    shares: float | None = None
    price: float | None = None
    value: float | None = None  # derived: shares * price
    post_transaction_amount: float | None = None
    sec_link: str | None = None  # URL to the underlying Form 4


class NewsArticleRow(FrozenRow):
    ticker: str
    published_at: datetime
    title: str
    url: str | None = None
    source_name: str | None = None
    content: str | None = None
    symbols: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    sentiment: float | None = None  # composite polarity in [-1, 1]
    sentiment_pos: float | None = None
    sentiment_neg: float | None = None
    sentiment_neu: float | None = None


class NewsSentimentRow(FrozenRow):
    """Daily aggregate sentiment for a ticker (typical vendor shape)."""

    ticker: str
    date: date
    sentiment: float | None = None
    article_count: int | None = None


class EarningsAnnouncementRow(FrozenRow):
    """One earnings event for a fiscal period.

    Holds both the actual reported result and the analyst consensus going in,
    so the row directly answers "did they beat?" via ``surprise_percent``.
    Future periods (not yet announced) have ``eps_actual`` = None but still
    carry ``eps_estimate`` and ``report_date``.

    ``before_after_market`` is normalized: vendors expose strings like
    EODHD's ``BeforeMarket`` / ``AfterMarket`` / ``DuringMarket``; adapters
    map them into ``before`` / ``after`` / ``during``.
    """

    ticker: str
    period_end: date
    report_date: date | None = None
    before_after_market: Literal["before", "during", "after"] | None = None
    currency: str | None = None
    eps_actual: float | None = None
    eps_estimate: float | None = None
    eps_difference: float | None = None
    surprise_percent: float | None = None


class AnalystForecastRow(FrozenRow):
    """Analyst dispersion, revenue forecasts, and EPS revision history for a
    given fiscal period.

    A single ``period_end`` can carry multiple rows when the vendor reports
    several relative offsets (e.g. the same quarter labelled both as
    ``current_quarter`` and ``current_year`` aggregates). ``period_relative``
    is the discriminator: vendors emit strings like EODHD's ``0q`` /
    ``+1q`` / ``+1y``; adapters map them into the literal below.
    """

    ticker: str
    period_end: date
    period_relative: (
        Literal[
            "prior_year",
            "prior_quarter",
            "current_quarter",
            "next_quarter",
            "current_year",
            "next_year",
        ]
        | None
    ) = None
    growth: float | None = None

    # EPS forecast dispersion
    eps_estimate_avg: float | None = None
    eps_estimate_low: float | None = None
    eps_estimate_high: float | None = None
    eps_estimate_year_ago: float | None = None
    eps_estimate_n_analysts: int | None = None
    eps_estimate_growth: float | None = None

    # Revenue forecast dispersion
    revenue_estimate_avg: float | None = None
    revenue_estimate_low: float | None = None
    revenue_estimate_high: float | None = None
    revenue_estimate_year_ago: float | None = None
    revenue_estimate_n_analysts: int | None = None
    revenue_estimate_growth: float | None = None

    # EPS estimate revision history
    eps_trend_current: float | None = None
    eps_trend_7d_ago: float | None = None
    eps_trend_30d_ago: float | None = None
    eps_trend_60d_ago: float | None = None
    eps_trend_90d_ago: float | None = None

    # Revision activity counts
    eps_revisions_up_7d: int | None = None
    eps_revisions_up_30d: int | None = None
    eps_revisions_down_7d: int | None = None
    eps_revisions_down_30d: int | None = None


class AnalystRatingsRow(FrozenRow):
    """Analyst consensus ratings + target price for a ticker, with an
    explicit ``snapshot_date`` so the row is a time series — not a
    single-row-per-ticker snapshot.

    Ingested via change-detection (``_upsert_on_change``) so the table only
    grows when the consensus actually moves.

    No single composite ``rating`` field — vendors use incompatible numeric
    scales for the same buckets (some 1=buy, some 1=sell), so we keep only
    the bucket counts (``strong_buy`` … ``strong_sell``) which are
    cross-vendor consistent. Consumers can compute their own consensus.
    """

    ticker: str
    snapshot_date: date
    target_price: float | None = None
    strong_buy: int = 0
    buy: int = 0
    hold: int = 0
    sell: int = 0
    strong_sell: int = 0


class InstitutionalHolderRow(FrozenRow):
    """One institutional / fund holder snapshot for a ticker.

    EODHD returns top-20 institutions and top-20 funds; the same row schema
    serves both, discriminated by ``holder_kind``. Ingested via
    change-detection: a new row is only inserted when one of the observed
    values (shares, assets %, change) actually moves.
    """

    ticker: str
    holder_kind: Literal["institution", "fund"]
    name: str
    snapshot_date: date
    # Stored as a percent in the 0–100 range, NOT a 0–1 fraction. EODHD's
    # ``totalShares`` / ``totalAssets`` keys already arrive as percentages
    # (e.g. ``9.7151`` means 9.72%, not 9.72×); other adapters must convert
    # before constructing this row.
    total_shares_pct: float | None = Field(default=None, ge=0.0, le=100.0)
    total_assets_pct: float | None = Field(default=None, ge=0.0, le=100.0)
    current_shares: int | None = None
    change_shares: int | None = None
    change_pct: float | None = None


class EsgSnapshotRow(FrozenRow):
    """ESG ratings header. Time-series via ``rating_date`` + change-detection."""

    ticker: str
    rating_date: date
    total_esg: float | None = None
    total_esg_percentile: float | None = None
    environment_score: float | None = None
    environment_percentile: float | None = None
    social_score: float | None = None
    social_percentile: float | None = None
    governance_score: float | None = None
    governance_percentile: float | None = None
    controversy_level: int | None = None


class EsgActivityRow(FrozenRow):
    """One controversial-activity flag for an ESG snapshot
    (e.g. ``activity='alcohol', involvement='No'``).

    Vendors use slightly different activity vocabularies; we keep both
    fields free-form so any vendor can populate without being locked to
    one taxonomy.
    """

    ticker: str
    rating_date: date
    activity: str
    # Closed cross-vendor set. Adapters normalize their raw vendor strings
    # (EODHD's ``"Yes"`` / ``"No"``) at parse time; rows where the vendor
    # returns anything else are dropped at the adapter boundary.
    involvement: Literal["yes", "no"]


class CrossListingRow(FrozenRow):
    """A secondary listing of the same security on another exchange.

    The row's ``ticker`` is the *canonical* (primary) ticker; ``exchange`` +
    ``exchange_code`` identify where else it trades.
    """

    ticker: str
    exchange: str
    exchange_code: str
    name: str | None = None


class OfficerRow(FrozenRow):
    """One executive on the current officer roster of an issuer.

    EODHD returns a current-state list with no per-officer date; we model
    this as a current-snapshot table (``upsert_officers`` deletes the
    ticker's existing officers before inserting), not a time series.
    """

    ticker: str
    name: str
    title: str | None = None
    year_born: int | None = None


class TickerSnapshotRow(FrozenRow):
    """Volatile metric snapshot for a ticker — extracted from
    ``TickerProfile`` so the static profile stays static and these
    drift-prone metrics get a proper time series.

    Ingested via change-detection.
    """

    ticker: str
    snapshot_date: date
    beta: float | None = None
    short_percent: float | None = None
    percent_insiders: float | None = None
    percent_institutions: float | None = None


class SharesOutstandingRow(FrozenRow):
    ticker: str
    date: date
    shares: float = Field(ge=0.0)


class StockSplitRow(FrozenRow):
    """One stock split event. ``ratio > 1`` is a forward split (2:1 → 2.0);
    ``ratio < 1`` is a reverse split (1:10 → 0.1)."""

    ticker: str
    date: date
    ratio: float = Field(gt=0.0)


class MarketCapRow(FrozenRow):
    ticker: str
    date: date
    market_cap: float = Field(ge=0.0)


class EmployeeCountRow(FrozenRow):
    ticker: str
    date: date
    count: int = Field(ge=0)


class SegmentationRow(FrozenRow):
    """Revenue or geographic segmentation line for a period.

    One row per (ticker, period_end, dimension, segment).
    """

    ticker: str
    period_end: date
    dimension: Literal["revenue", "geographic"]
    segment: str
    value: float | None = None


# ---- static profile --------------------------------------------------------


class TickerProfile(FrozenRow):
    """Static-ish metadata about a ticker. Refreshed periodically but not a
    time series — volatile metrics like ownership percentages, beta and
    short interest live on :class:`TickerSnapshotRow` instead.

    ``security_type`` is the vendor-agnostic kind (``common_stock``,
    ``preferred_stock``, ``adr``, ``etf``, ``fund``, ``other``); adapters
    map their vendor strings (e.g. EODHD's ``Common Stock`` / ``ETF``)
    into this literal.
    """

    id: str  # canonical ticker (e.g. AAPL.US)
    asset_class: AssetClass = "equity"
    exchange: str | None = None
    currency: str | None = None
    name: str | None = None

    # Country + classification
    country_iso: str | None = None
    sector: str | None = None
    industry: str | None = None
    gic_sector: str | None = None
    gic_group: str | None = None
    gic_industry: str | None = None
    gic_sub_industry: str | None = None

    # Lifecycle
    ipo_date: date | None = None
    is_delisted: bool = False
    delisted_date: date | None = None
    is_bank: bool = False
    fiscal_year_end: str | None = None
    security_type: (
        Literal["common_stock", "preferred_stock", "adr", "etf", "fund", "other"] | None
    ) = None

    # Identifiers
    cusip: str | None = None
    cik: str | None = None
    isin: str | None = None
    open_figi: str | None = None
    lei: str | None = None
    employer_id_number: str | None = None
    primary_ticker: str | None = None

    # Address + contact
    address_street: str | None = None
    address_city: str | None = None
    address_state: str | None = None
    address_country: str | None = None
    address_zip: str | None = None
    phone: str | None = None
    web_url: str | None = None

    # Misc
    description: str | None = None
    updated_at: datetime | None = None

    @model_validator(mode="after")
    def _security_type_is_equity_only(self) -> TickerProfile:
        # ``security_type`` is the equity sub-kind (common_stock / etf /
        # …); non-equity rows must leave it ``None``. Catches the bug
        # where an equity profile parser is reused for a non-equity
        # ticker and the vendor's ``Type`` field leaks into the equity
        # column (see review C2).
        if self.asset_class != "equity" and self.security_type is not None:
            raise ValueError(
                f"security_type is equity-only; got "
                f"asset_class={self.asset_class!r} security_type={self.security_type!r}"
            )
        return self


# ---- per-asset-class profile rows ------------------------------------------


class CryptoProfileRow(FrozenRow):
    """Static-ish profile for a crypto asset (one row per ticker).

    The equity-shaped tables (fundamentals, dividends, …) don't apply to
    crypto, so this is the small, focused surface a crypto strategy actually
    needs: identity (base/quote symbol, blockchain, consensus) and supply.
    Refreshed periodically via change-detection-equivalent ON CONFLICT
    UPDATE; supply *trajectory* is captured by re-snapshotting the row, not
    by a separate time series — most strategies care about current
    circulating supply, not its weekly history.

    ``consensus_type`` is normalized at the adapter boundary; vendors emit
    strings like "Proof of Work" / "PoS" — adapters map them to the closed
    Literal below so SQL filters stay portable.

    The ``max_supply >= total_supply >= circulating_supply`` invariant is
    enforced when each pair is non-``None``: the row exists precisely to
    surface the data-quality bug a vendor returning, e.g., total > max
    after a chain fork would represent. ``None``-on-either-side is left
    alone (legitimate partial fill — the vendor only reports circulating).
    """

    ticker: str
    base_symbol: str | None = None
    quote_symbol: str | None = None
    blockchain: str | None = None
    consensus_type: CryptoConsensusType | None = None
    circulating_supply: float | None = Field(default=None, ge=0.0)
    total_supply: float | None = Field(default=None, ge=0.0)
    max_supply: float | None = Field(default=None, ge=0.0)
    supply_snapshot_date: date | None = None

    @model_validator(mode="after")
    def _validate_supply_chain(self) -> CryptoProfileRow:
        if (
            self.total_supply is not None
            and self.circulating_supply is not None
            and self.total_supply < self.circulating_supply
        ):
            raise ValueError(
                f"total_supply ({self.total_supply}) < circulating_supply "
                f"({self.circulating_supply}) for {self.ticker}"
            )
        if (
            self.max_supply is not None
            and self.total_supply is not None
            and self.max_supply < self.total_supply
        ):
            raise ValueError(
                f"max_supply ({self.max_supply}) < total_supply "
                f"({self.total_supply}) for {self.ticker}"
            )
        return self


class BondProfileRow(FrozenRow):
    """Static profile for a bond (one row per ticker).

    Bonds don't have fundamentals in the equity sense; they have terms
    (coupon, frequency, maturity) and an issuer. Yield + price move daily
    and live in :class:`BondYieldRow` instead — this row is the static
    contract.

    ``issuer_kind`` and ``bond_kind`` are normalized at the adapter
    boundary to small closed Literals so strategies can group/filter
    portably across vendors.
    """

    ticker: str
    issuer_name: str | None = None
    issuer_kind: BondIssuerKind | None = None
    bond_kind: BondKind | None = None
    # ``coupon_rate`` is annual % (zero-coupon bonds use 0.0). The
    # ``le=100`` cap catches the canonical decimal-vs-percent confusion
    # bug — a vendor returning 0.0425 instead of 4.25% would otherwise
    # silently mis-classify the bond as low-coupon.
    coupon_rate: float | None = Field(default=None, ge=0.0, le=100.0)
    coupon_frequency: int | None = Field(default=None, ge=0, le=365)  # payments per year
    face_value: float | None = Field(default=None, ge=0.0)
    currency: str | None = None
    issue_date: date | None = None
    maturity_date: date | None = None
    credit_rating: str | None = None  # free-text — S&P/Moody's vocabularies differ


class BondYieldRow(FrozenRow):
    """One day's observed yield + clean price for a bond.

    Time-series, keyed by (ticker, date). Yield-to-maturity is in percent;
    clean price is quoted as percent-of-par (so 99.45 means 99.45% of face).
    """

    ticker: str
    date: date
    yield_to_maturity: float | None = None
    clean_price: float | None = None


class CommodityContractRow(FrozenRow):
    """Contract metadata for a commodity instrument (futures, spot,
    continuous, or index).

    The bar series itself lives in ``bars`` with the ticker as the access
    key; this row carries what *kind* of commodity exposure that ticker
    represents and the standardized contract details. ``contract_kind`` is
    normalized at the adapter boundary.

    For continuous front-month synthetic series, ``contract_month`` and
    ``expiry_date`` are typically null; for a specific futures contract
    they identify the delivery period.
    """

    ticker: str
    underlying_symbol: str | None = None  # e.g. 'GC' for gold futures
    contract_kind: CommodityContractKind | None = None
    # 'YYYY-MM' for specific futures; pinned by regex so downstream
    # parsers don't each have to re-validate the format.
    contract_month: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}$")
    expiry_date: date | None = None
    contract_size: float | None = Field(default=None, ge=0.0)
    contract_unit: str | None = None  # 'troy_ounce', 'barrel', 'metric_ton', …


# ---- macroeconomic indicators ----------------------------------------------

# Reporting cadence vendors expose macro series at. Closed cross-vendor set;
# adapters map their raw strings ("Annual", "Quarterly", …) into these
# lower-case literals at parse time. Rows whose vendor returns anything
# outside this set drop ``period`` to ``None`` rather than rejecting the
# observation — the value itself is still useful even when the cadence
# tag isn't classifiable.
MacroPeriod = Literal["annual", "quarterly", "monthly"]


class MacroIndicatorRow(FrozenRow):
    """One observation of a macroeconomic indicator for a country.

    Country-level time series: keyed by ``(country_iso, indicator,
    observation_date)`` so one country can carry many indicators and a
    single indicator can span many countries inside the same table.

    ``country_iso`` is ISO 3166-1 alpha-3 ("USA", "DEU", "GBR"); adapters
    that receive ISO-2 should expand before constructing the row so the
    PK stays consistent across vendors.

    ``indicator`` is a canonical lower_snake_case key (e.g.
    ``"real_gdp_total"``, ``"inflation_consumer_prices_annual"``,
    ``"unemployment_total_percent"``). The set is open — vendors keep
    adding new series — so this stays free-text rather than a closed
    Literal; adapters normalize their vendor strings into snake_case at
    parse time so SQL filters stay portable.
    """

    country_iso: str = Field(min_length=3, max_length=3)
    indicator: str = Field(min_length=1)
    observation_date: date
    period: MacroPeriod | None = None
    country_name: str | None = None
    value: float | None = None


# ---- DeFi total value locked -------------------------------------------------


class DefiTvlRow(FrozenRow):
    """One daily total-value-locked observation for a blockchain.

    Keyed by ``(chain, observation_date)``. ``chain`` is the canonical
    lower-case chain name (``"ethereum"``, ``"solana"``); adapters normalize
    their vendor spelling before constructing the row so the key is stable
    across vendors. ``observation_date`` is the UTC calendar day the vendor
    stamps the observation with; it is *not* the day the value became
    public, so point-in-time readers must add their own lag. ``tvl_usd`` is
    the aggregate DeFi TVL in US dollars; ``source`` records which adapter
    wrote the row (provenance only, not part of the key).
    """

    chain: str = Field(min_length=1, pattern=r"^[^A-Z]+$")
    observation_date: date
    tvl_usd: float | None = Field(default=None, ge=0.0)
    source: str = Field(min_length=1)


# ---- source discovery (not a lake-row type) --------------------------------


class ExchangeInfo(FrozenRow):
    """One exchange supported by a :class:`DataSource`. Returned by
    :meth:`DataSource.list_exchanges` for human discovery — not persisted to
    the lake."""

    code: str
    name: str | None = None
    country: str | None = None
    currency: str | None = None
    country_iso2: str | None = None
    country_iso3: str | None = None
    operating_mic: str | None = None


class SymbolListing(FrozenRow):
    """One symbol a :class:`DataSource` lists on an exchange, active or
    delisted. Returned by :meth:`DataSource.list_symbols`; exchange
    universes turn listings into membership spans and instrument rows.

    ``security_type`` is the canonical equity sub-kind (``None`` for other
    asset classes or when the vendor does not say). ``is_delisted`` is
    ``True`` for symbols the vendor reports as no longer trading."""

    ticker: str
    exchange: str | None = None
    name: str | None = None
    currency: str | None = None
    country: str | None = None
    asset_class: AssetClass = "equity"
    security_type: SecurityType | None = None
    isin: str | None = None
    is_delisted: bool = False

    @model_validator(mode="after")
    def _security_type_is_equity_only(self) -> SymbolListing:
        if self.asset_class != "equity" and self.security_type is not None:
            raise ValueError("security_type is equity-only")
        return self

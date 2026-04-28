-- Split fundamentals into one wide table per financial statement.
--
-- The old `fundamentals` table was a long-form key-value table
-- (statement, line_item, value) — every column type collapsed to DOUBLE
-- and consumers had to PIVOT to read a single period. Each statement
-- has a stable, well-known set of line items, so a wide table per
-- statement is the natural shape:
--   * one row per (ticker, period_end, frequency)
--   * each line item is its own typed column
--   * misspelled or unknown vendor fields don't pollute the schema
--
-- We're pre-production, so this is a clean break — drop the old table
-- rather than carry a back-compat view. New code paths use the three
-- new tables directly.
DROP TABLE IF EXISTS fundamentals;

-- ---- income statement -----------------------------------------------------

CREATE TABLE income_statement (
    ticker                          VARCHAR NOT NULL,
    period_end                      DATE    NOT NULL,
    frequency                       VARCHAR NOT NULL,   -- 'Q' | 'A'
    filing_date                     DATE,
    currency                        VARCHAR,

    -- Top of statement
    revenue                         DOUBLE,
    cost_of_revenue                 DOUBLE,
    gross_profit                    DOUBLE,

    -- Operating expense breakdown
    research_development            DOUBLE,
    selling_general_administrative  DOUBLE,
    selling_marketing_expenses      DOUBLE,
    other_operating_expenses        DOUBLE,
    total_operating_expenses        DOUBLE,

    -- Operating income
    operating_income                DOUBLE,

    -- Non-operating
    interest_income                 DOUBLE,
    interest_expense                DOUBLE,
    net_interest_income             DOUBLE,
    non_operating_income_other      DOUBLE,
    total_other_income_expense_net  DOUBLE,

    -- Pre-tax / tax
    income_before_tax               DOUBLE,
    income_tax_expense              DOUBLE,
    tax_provision                   DOUBLE,

    -- Bottom of statement
    minority_interest               DOUBLE,
    net_income_continuing           DOUBLE,
    discontinued_operations         DOUBLE,
    extraordinary_items             DOUBLE,
    non_recurring                   DOUBLE,
    other_items                     DOUBLE,
    effect_of_accounting_charges    DOUBLE,
    net_income                      DOUBLE,
    net_income_to_common            DOUBLE,
    preferred_stock_adjustments     DOUBLE,

    -- Computed/normalized lines vendors expose alongside the raw ones.
    -- Kept (rather than re-derived downstream) because vendors apply
    -- their own one-time-item exclusions, so the vendor's EBIT/EBITDA
    -- is the value most ratio comparisons key off.
    ebit                            DOUBLE,
    ebitda                          DOUBLE,
    depreciation_amortization       DOUBLE,
    reconciled_depreciation         DOUBLE,

    PRIMARY KEY (ticker, period_end, frequency)
);

-- ---- balance sheet --------------------------------------------------------

CREATE TABLE balance_sheet (
    ticker                                  VARCHAR NOT NULL,
    period_end                              DATE    NOT NULL,
    frequency                               VARCHAR NOT NULL,
    filing_date                             DATE,
    currency                                VARCHAR,

    -- Asset side
    total_assets                            DOUBLE,
    current_assets                          DOUBLE,
    cash                                    DOUBLE,
    cash_and_equivalents                    DOUBLE,
    cash_and_short_term_investments         DOUBLE,
    short_term_investments                  DOUBLE,
    net_receivables                         DOUBLE,
    inventory                               DOUBLE,
    other_current_assets                    DOUBLE,

    non_current_assets                      DOUBLE,
    long_term_investments                   DOUBLE,
    property_plant_equipment_net            DOUBLE,
    property_plant_equipment_gross          DOUBLE,
    accumulated_depreciation                DOUBLE,
    accumulated_amortization                DOUBLE,
    goodwill                                DOUBLE,
    intangible_assets                       DOUBLE,
    other_assets                            DOUBLE,
    deferred_long_term_asset_charges        DOUBLE,
    non_current_assets_other                DOUBLE,
    earning_assets                          DOUBLE,

    -- Liability side
    total_liabilities                       DOUBLE,
    current_liabilities                     DOUBLE,
    accounts_payable                        DOUBLE,
    current_deferred_revenue                DOUBLE,
    short_term_debt                         DOUBLE,
    short_long_term_debt                    DOUBLE,
    short_long_term_debt_total              DOUBLE,
    other_current_liabilities               DOUBLE,

    non_current_liabilities                 DOUBLE,
    long_term_debt                          DOUBLE,
    long_term_debt_total                    DOUBLE,
    capital_lease_obligations               DOUBLE,
    deferred_long_term_liabilities          DOUBLE,
    other_liabilities                       DOUBLE,
    non_current_liabilities_other           DOUBLE,
    negative_goodwill                       DOUBLE,
    warrants                                DOUBLE,
    preferred_stock_redeemable              DOUBLE,

    -- Equity
    total_stockholder_equity                DOUBLE,
    common_stock                            DOUBLE,
    capital_stock                           DOUBLE,
    additional_paid_in_capital              DOUBLE,
    retained_earnings                       DOUBLE,
    treasury_stock                          DOUBLE,
    accumulated_other_comprehensive_income  DOUBLE,
    other_stockholder_equity                DOUBLE,
    common_stock_total_equity               DOUBLE,
    preferred_stock_total_equity            DOUBLE,
    retained_earnings_total_equity          DOUBLE,
    capital_surplus                         DOUBLE,
    total_permanent_equity                  DOUBLE,
    noncontrolling_interest                 DOUBLE,
    temporary_equity_redeemable_noncontrolling DOUBLE,
    liabilities_and_stockholders_equity     DOUBLE,

    -- Aggregates / derived (vendor-supplied)
    net_debt                                DOUBLE,
    net_tangible_assets                     DOUBLE,
    net_working_capital                     DOUBLE,
    investments                             DOUBLE,
    common_stock_shares_outstanding         DOUBLE,

    PRIMARY KEY (ticker, period_end, frequency)
);

-- ---- cash flow statement --------------------------------------------------

CREATE TABLE cash_flow_statement (
    ticker                              VARCHAR NOT NULL,
    period_end                          DATE    NOT NULL,
    frequency                           VARCHAR NOT NULL,
    filing_date                         DATE,
    currency                            VARCHAR,

    -- Headline subtotals
    operating_cash_flow                 DOUBLE,
    investing_cash_flow                 DOUBLE,
    financing_cash_flow                 DOUBLE,

    -- Operating section detail
    net_income                          DOUBLE,
    depreciation                        DOUBLE,
    stock_based_compensation            DOUBLE,
    change_in_working_capital           DOUBLE,
    change_to_inventory                 DOUBLE,
    change_to_account_receivables       DOUBLE,
    change_to_liabilities               DOUBLE,
    change_to_operating_activities      DOUBLE,
    change_to_net_income                DOUBLE,
    change_receivables                  DOUBLE,
    cash_flows_other_operating          DOUBLE,
    other_non_cash_items                DOUBLE,

    -- Investing section detail
    capital_expenditures                DOUBLE,
    investments                         DOUBLE,
    other_cash_flows_investing          DOUBLE,

    -- Financing section detail
    dividends_paid                      DOUBLE,
    net_borrowings                      DOUBLE,
    issuance_of_capital_stock           DOUBLE,
    sale_purchase_of_stock              DOUBLE,
    other_cash_flows_financing          DOUBLE,

    -- Period reconciliation
    change_in_cash                      DOUBLE,
    cash_and_cash_equivalents_changes   DOUBLE,
    begin_period_cash_flow              DOUBLE,
    end_period_cash_flow                DOUBLE,
    exchange_rate_changes               DOUBLE,

    -- Vendor-derived
    free_cash_flow                      DOUBLE,

    PRIMARY KEY (ticker, period_end, frequency)
);

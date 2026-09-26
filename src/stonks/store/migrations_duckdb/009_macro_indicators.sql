-- Macroeconomic indicators: country-level time series for GDP, inflation,
-- unemployment, trade balance, etc. Sourced per (country_iso, indicator,
-- observation_date) so a single country can carry many indicators and a
-- single indicator spans many countries.
--
-- Vendor-agnostic shape: the lake never sees vendor field names. EODHD's
-- /macro-indicator endpoint exposes ~30 indicators identified by snake_case
-- keys (real_gdp_total, inflation_consumer_prices_annual, …); other
-- adapters must map their own vocabulary onto the same canonical strings.
-- The set is open (vendors add new ones) so `indicator` stays free-text
-- rather than a closed enum/literal — matches the same trade-off we made
-- for `currency` on the financial-statement tables.
--
-- `period` is normalized at the adapter boundary into the canonical lower-
-- case set {annual, quarterly, monthly}; values outside that set are
-- dropped at parse time so the column stays well-typed without inviting a
-- closed-enum migration every time a vendor introduces a new cadence.

CREATE TABLE macro_indicators (
    country_iso       VARCHAR NOT NULL,    -- ISO 3166-1 alpha-3, e.g. 'USA'
    indicator         VARCHAR NOT NULL,    -- canonical snake_case key
    observation_date  DATE    NOT NULL,    -- the date the data point describes
    period            VARCHAR,             -- 'annual' | 'quarterly' | 'monthly'
    country_name      VARCHAR,             -- free-text, vendor-supplied label
    value             DOUBLE,              -- the observation itself
    PRIMARY KEY (country_iso, indicator, observation_date)
);

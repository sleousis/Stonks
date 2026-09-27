-- One home for each account fact (review leftovers 19.18).
--
-- The jurisdiction lived in portfolio_tax_settings and account_profiles,
-- and the base currency in portfolios and account_profiles, with nothing
-- keeping them in step. From here on:
--   * portfolio_tax_settings.jurisdiction is the jurisdiction,
--   * portfolios.base_currency is the base currency,
--   * portfolio_tax_settings.wash_sales says whether wash sales apply at
--     all, and account_profiles.wash_sale_mode only says what the pre-trade
--     guard does then (warn or block).
-- account_profiles reads the first two through them and loses its copies.
--
-- Existing rows are reconciled first: the newer of the profile and the tax
-- settings (by updated_at) wins. A profile without tax settings wins.

-- 1. Base currency (before the tax rows change their updated_at).
UPDATE portfolios
   SET base_currency = (SELECT upper(p.base_currency) FROM account_profiles p
                         WHERE p.portfolio_id = portfolios.id)
 WHERE id IN (
       SELECT p.portfolio_id FROM account_profiles p
         LEFT JOIN portfolio_tax_settings t ON t.portfolio_id = p.portfolio_id
        WHERE t.portfolio_id IS NULL OR p.updated_at > t.updated_at);

-- 2. Jurisdiction: a newer profile overwrites the tax row ...
UPDATE portfolio_tax_settings
   SET jurisdiction = (SELECT p.jurisdiction FROM account_profiles p
                        WHERE p.portfolio_id = portfolio_tax_settings.portfolio_id),
       updated_at = (SELECT p.updated_at FROM account_profiles p
                      WHERE p.portfolio_id = portfolio_tax_settings.portfolio_id),
       updated_by = (SELECT p.updated_by FROM account_profiles p
                      WHERE p.portfolio_id = portfolio_tax_settings.portfolio_id)
 WHERE portfolio_id IN (
       SELECT p.portfolio_id FROM account_profiles p
         JOIN portfolio_tax_settings t ON t.portfolio_id = p.portfolio_id
        WHERE p.updated_at > t.updated_at);

-- ... and a profile with no tax row gets one (the tax defaults otherwise).
INSERT INTO portfolio_tax_settings (portfolio_id, jurisdiction, lot_method, wash_sales,
                                    updated_at, updated_by)
SELECT p.portfolio_id, p.jurisdiction, 'fifo', 1, p.updated_at, p.updated_by
  FROM account_profiles p
 WHERE NOT EXISTS (SELECT 1 FROM portfolio_tax_settings t
                    WHERE t.portfolio_id = p.portfolio_id);

-- 3. Rebuild account_profiles without the copies.
CREATE TABLE account_profiles_new (
    portfolio_id   TEXT PRIMARY KEY REFERENCES portfolios(id),
    account_type   TEXT NOT NULL DEFAULT 'cash' CHECK (account_type IN ('cash', 'margin')),
    client_class   TEXT NOT NULL DEFAULT 'retail'
                   CHECK (client_class IN ('retail', 'professional')),
    fx_policy      TEXT NOT NULL DEFAULT 'refuse' CHECK (fx_policy IN ('refuse', 'convert')),
    wash_sale_mode TEXT NOT NULL DEFAULT 'warn' CHECK (wash_sale_mode IN ('warn', 'block')),
    allow_short    INTEGER NOT NULL DEFAULT 0 CHECK (allow_short IN (0, 1)),
    updated_at     TEXT NOT NULL,
    updated_by     TEXT NOT NULL CHECK (length(trim(updated_by)) > 0),
    -- shorts need a margin account
    CHECK (allow_short = 0 OR account_type = 'margin')
);

INSERT INTO account_profiles_new (portfolio_id, account_type, client_class, fx_policy,
                                  wash_sale_mode, allow_short, updated_at, updated_by)
SELECT portfolio_id, account_type, client_class, fx_policy, wash_sale_mode, allow_short,
       updated_at, updated_by
  FROM account_profiles;

DROP TABLE account_profiles;
ALTER TABLE account_profiles_new RENAME TO account_profiles;

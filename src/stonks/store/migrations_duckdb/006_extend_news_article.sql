-- Capture richer per-article news fields the vendor already returns.
--
-- - content: full article body. Schema column added now; the EODHD adapter
--   deliberately leaves it empty for the time being (see parse_news_response)
--   to avoid inflating the lake without a current consumer. Future flip
--   needs no migration.
-- - symbols / tags: cross-ticker linkage and topic categorization. First
--   array-typed columns in the lake (VARCHAR[]). Used here because both
--   sets are unbounded per article and a junction table would add
--   significant complexity for little gain.
-- - sentiment_pos / sentiment_neg / sentiment_neu: full sentiment breakdown
--   alongside the existing composite polarity (`sentiment`).

ALTER TABLE news ADD COLUMN content       VARCHAR;
ALTER TABLE news ADD COLUMN symbols       VARCHAR[];
ALTER TABLE news ADD COLUMN tags          VARCHAR[];
ALTER TABLE news ADD COLUMN sentiment_pos DOUBLE;
ALTER TABLE news ADD COLUMN sentiment_neg DOUBLE;
ALTER TABLE news ADD COLUMN sentiment_neu DOUBLE;

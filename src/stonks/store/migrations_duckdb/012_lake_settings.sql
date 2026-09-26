-- Lake-level settings the lake itself must remember, so every process that
-- opens the file (API, CLI, MCP, lab workers) agrees on them without extra
-- configuration. Today one key: ``bars_backend`` ('duckdb' | 'parquet'),
-- which bar store holds the OHLCV bars (roadmap 10.4). No row means the
-- default, the ``bars`` table ('duckdb'). Rows are written by
-- ``DuckDBLake.migrate_bars_to_parquet`` / ``migrate_bars_to_duckdb``.
CREATE TABLE lake_settings (
    key   VARCHAR PRIMARY KEY,
    value VARCHAR NOT NULL
);

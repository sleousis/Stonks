# Stonks

End-to-end equity research + trading system. Ingests prices/fundamentals from vendor APIs into a local DuckDB "lake", lets a `Strategy` class ride through a lab (tune → fit → backtest → survival suite), registers survivors in SQLite state, and runs a cron-triggered production tick that ranks active strategies and (eventually) places real orders through a pluggable broker.

## Quick start

```bash
uv sync
cp .env.example .env        # fill in EODHD_API_KEY
uv run stonks db init
uv run stonks ingest prices --tickers AAPL.US --since 2025-05-01
uv run stonks db info
```

## Architecture

See [`docs/architecture.md`](docs/architecture.md) and the per-block documents under [`docs/blocks/`](docs/blocks/).

## Testing

```bash
uv run pytest                               # unit + integration (no network)
STONKS_RUN_LIVE_TESTS=1 uv run pytest       # also runs live contract tests
uv run ruff check .
```

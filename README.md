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

## API docs

Generated from the code, published on every push to `main`:

- Site: <https://sleousis.github.io/Stonks/> ([REST, Swagger UI](https://sleousis.github.io/Stonks/rest.html), [MCP tools](https://sleousis.github.io/Stonks/mcp.html))
- Markdown: [`docs/api/rest.md`](docs/api/rest.md) and [`docs/api/mcp-tools.md`](docs/api/mcp-tools.md)

A running `stonks serve` also serves live Swagger UI at `/docs` (ReDoc at `/redoc`, spec at `/openapi.json`). Use "Authorize" with `STONKS_API_TOKEN` for non-GET calls.

## Testing

```bash
uv run pytest                               # unit + integration (no network)
STONKS_RUN_LIVE_TESTS=1 uv run pytest       # also runs live contract tests
uv run ruff check .
```

## Upgrading an older lake

`stonks db init` refuses to run a migration that would drop populated data. Migration 005 drops six legacy columns from `tickers` (now `instruments`) without copying them, so a lake created before it and still holding those values needs `STONKS_ALLOW_DESTRUCTIVE_MIGRATIONS=1`. Back up the lake file first. Fresh lakes are unaffected.

## Roadmap

See `docs/roadmap.md`.

## Deploy and contribute

Run Stonks on a server with Docker Compose: see `docs/deploy.md`. How to contribute: `CONTRIBUTING.md`. Changes per release: `CHANGELOG.md`.

## License and disclaimer

All rights reserved; see `LICENSE`. This is not financial advice; read `DISCLAIMER.md` before trading with it.

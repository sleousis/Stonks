# MCP server (`stonks mcp`)

`stonks mcp` runs a [Model Context Protocol](https://modelcontextprotocol.io)
server over stdio so Claude Code, Claude Desktop or any MCP client can read the
portfolio, inspect strategies and market data, launch backtests, lab runs and
ingests, and (with explicit confirmation) change strategy status or queue a
production tick.

## How it works

DuckDB allows one writing process, and `stonks serve` holds the lake open. So
the MCP server never opens the lake or the state database: it is a thin client
of the running REST API. Start the API first:

```bash
export STONKS_API_TOKEN=...      # same value for serve and mcp
uv run stonks serve              # http://127.0.0.1:8000
```

If the API is not reachable, every tool returns an error that says so and
tells you to run `stonks serve`.

## Configuration

| Setting | Where | Default | Purpose |
|---------|-------|---------|---------|
| `[mcp].api_url` | `config/default.toml` | `http://127.0.0.1:8000` | Base URL of the REST API |
| `[mcp].timeout_seconds` | `config/default.toml` | `30` | Per-request HTTP timeout |
| `[mcp].max_wait_seconds` | `config/default.toml` | `600` | Upper bound for `wait_for_job` |
| `STONKS_API_TOKEN` | env / `.env` | unset | Bearer token sent to the API |

The token is env-only. Without it the read tools work (reads are open on
loopback by default), and job and write tools fail with a message asking for
it. The token is never sent over plain `http` to a non-loopback host:
`stonks mcp` exits with an error instead (use `https`). `[mcp]` is read from
`config/default.toml` in the working directory, so start the server from the
repo root (the `--directory` / `cwd` settings below do that).

## Register with Claude Code

From the repo root:

```bash
claude mcp add stonks --env STONKS_API_TOKEN=your-token -- uv run --directory "$(pwd)" stonks mcp
```

Or put the token in the repo's `.env` (`stonks mcp` loads it) and drop
`--env`. Check it with `claude mcp list`, or `/mcp` inside a session.

## Register with Claude Desktop

Add to `claude_desktop_config.json` (Settings, Developer, Edit Config):

```json
{
  "mcpServers": {
    "stonks": {
      "command": "uv",
      "args": ["run", "--directory", "C:\\path\\to\\Stonks", "stonks", "mcp"],
      "env": { "STONKS_API_TOKEN": "your-token" }
    }
  }
}
```

Restart Claude Desktop after editing. `stonks serve` must be running.

## Tools

Read (read-only, idempotent):

| Tool | Route |
|------|-------|
| `health` | `GET /api/health` |
| `get_portfolio`, `list_portfolio_snapshots` | `GET /api/portfolio[/snapshots]` |
| `list_strategies`, `get_strategy` (with survival reports) | `GET /api/strategies[/{id}]` |
| `search_instruments`, `get_bars`, `get_coverage` | `GET /api/market/...` |
| `list_orders`, `list_fills` | `GET /api/orders[/fills]` |
| `list_ticks`, `get_tick` | `GET /api/ticks[/{id}]` |
| `list_ingest_runs` | `GET /api/ingest/runs` |
| `get_catalog` (strategy classes + parameter specs, intervals, asset classes) | `GET /api/catalog/...` |
| `list_jobs`, `get_job`, `wait_for_job` | `GET /api/jobs[/{id}]` |

Jobs (not destructive; return a job, follow with `wait_for_job`):
`run_backtest`, `run_lab`, `run_ingest`.

Guarded writes (destructive hint, need `confirm: true`):
`promote_strategy`, `shadow_strategy`, `retire_strategy`, `run_tick`.

Resource: `stonks://portfolio/summary` (cash, total value, positions).

## Safety model

- Every guarded tool called without `confirm: true` returns a preview (current
  and new status, survival results and warnings, or the tick request, active
  strategies and broker mode) and sends nothing mutating. Clients should show
  the preview to the user before confirming.
- `run_tick` defaults to `dry_run: true`. A real tick (`dry_run: false`) is
  refused unless the API positively reports a simulated or paper broker; if
  the API cannot say (the broker-status route is not there yet), MCP refuses.
  Live ticks belong in the CLI or UI.
- No tool changes broker settings or configuration, or enables live trading.
- The token is only sent as a bearer header. Tool output and errors are
  redacted, and logs go to stderr (stdout carries the MCP protocol).
- The API enforces auth itself: every mutating route needs the token whatever
  the client.

## Adding a tool

A tool is one small async function in `src/stonks/mcp/server.py` in the
matching `_register_*` block: pick the annotation constant (`READ`, `JOB`,
`STATUS_CHANGE`, `TICK`), give it a docstring (the tool description) and typed
parameters (the input schema), and return `await call(api.get(...))`. Add a
test in `tests/integration/app/test_mcp_server.py`.

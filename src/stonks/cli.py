"""Typer CLI entrypoint — ``stonks`` command.

Subcommands: ``db init``, ``db info``, ``ingest prices``, ``ingest fundamentals``.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from stonks.config import Settings, load_settings
from stonks.ingest.pipeline import IngestPipeline
from stonks.ingest.sources.base import DataSource
from stonks.ingest.sources.eodhd import EodhdDataSource
from stonks.logging import configure_logging, get_logger
from stonks.store.lake import DuckDBLake
from stonks.store.state import SqliteState

app = typer.Typer(add_completion=False, help="Stonks CLI")
db_app = typer.Typer(help="Database / lake operations")
ingest_app = typer.Typer(help="Data ingestion")
app.add_typer(db_app, name="db")
app.add_typer(ingest_app, name="ingest")

console = Console()


# ---- helpers ----------------------------------------------------------------


def _settings() -> Settings:
    settings = load_settings()
    configure_logging(level=settings.logging.level)
    return settings


def _build_source(settings: Settings) -> DataSource:
    api_key = settings.sources.eodhd.api_key
    if not api_key:
        raise typer.BadParameter(
            "EODHD_API_KEY is not set (add it to .env or your shell environment)"
        )
    return EodhdDataSource(
        api_key=api_key,
        base_url=settings.sources.eodhd.base_url,
        timeout_seconds=settings.sources.eodhd.timeout_seconds,
        max_retries=settings.sources.eodhd.max_retries,
        retry_backoff_seconds=settings.sources.eodhd.retry_backoff_seconds,
    )


def _open_lake(path: Path) -> DuckDBLake:
    return DuckDBLake(path)


def _parse_tickers(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [t.strip() for t in raw.split(",") if t.strip()]


# ---- db ---------------------------------------------------------------------


@db_app.command("init")
def db_init() -> None:
    """Create the lake + state files and apply pending migrations to each."""
    settings = _settings()
    with _open_lake(settings.lake.path) as lake:
        lake.migrate()
        lake_versions = lake.applied_migrations()
    with SqliteState(settings.state.path) as state:
        state.migrate()
        state_versions = state.applied_migrations()
    console.print(
        f"[green]lake ready[/green]  {settings.lake.path} (migrations: {lake_versions})"
    )
    console.print(
        f"[green]state ready[/green] {settings.state.path} (migrations: {state_versions})"
    )


@db_app.command("info")
def db_info() -> None:
    """Print tables and row counts for both the lake and the state DB."""
    settings = _settings()

    lake_table = Table(title=f"lake — {settings.lake.path}")
    lake_table.add_column("table")
    lake_table.add_column("rows", justify="right")
    with _open_lake(settings.lake.path) as lake:
        for name in sorted(lake.tables()):
            lake_table.add_row(name, str(lake.count_rows(name)))
    console.print(lake_table)

    state_table = Table(title=f"state — {settings.state.path}")
    state_table.add_column("table")
    state_table.add_column("rows", justify="right")
    with SqliteState(settings.state.path) as state:
        for name in sorted(state.tables()):
            state_table.add_row(name, str(state.count_rows(name)))
    console.print(state_table)


# ---- ingest -----------------------------------------------------------------


@ingest_app.command("prices")
def ingest_prices(
    tickers: str | None = typer.Option(
        None, "--tickers", help="comma-separated tickers, e.g. AAPL.US,MSFT.US"
    ),
    exchange: str | None = typer.Option(
        None, "--exchange", help="fetch all tickers on this exchange (e.g. US)"
    ),
    since: str | None = typer.Option(
        None, "--since", help="earliest date (YYYY-MM-DD); omit to fetch full history"
    ),
    until: str | None = typer.Option(
        None, "--until", help="latest date (YYYY-MM-DD)"
    ),
) -> None:
    settings = _settings()
    source = _build_source(settings)

    with _open_lake(settings.lake.path) as lake:
        lake.migrate()
        ticker_list = _parse_tickers(tickers)
        if not ticker_list and exchange:
            ticker_list = source.list_tickers(exchange)
        if not ticker_list:
            raise typer.BadParameter("provide --tickers or --exchange")

        since_d = date.fromisoformat(since) if since else None
        until_d = date.fromisoformat(until) if until else None

        pipeline = IngestPipeline(source=source, lake=lake)
        result = pipeline.run_prices(ticker_list, since=since_d, until=until_d)

    _print_result(result)


@ingest_app.command("fundamentals")
def ingest_fundamentals(
    tickers: str = typer.Option(..., "--tickers", help="comma-separated tickers"),
) -> None:
    settings = _settings()
    source = _build_source(settings)

    with _open_lake(settings.lake.path) as lake:
        lake.migrate()
        ticker_list = _parse_tickers(tickers)
        pipeline = IngestPipeline(source=source, lake=lake)
        result = pipeline.run_fundamentals(ticker_list)

    _print_result(result)


def _print_result(result) -> None:
    color = {"ok": "green", "partial": "yellow", "error": "red"}.get(result.status, "white")
    console.print(
        f"[{color}]run #{result.run_id} — kind={result.kind} status={result.status} "
        f"ok={result.tickers_ok} failed={result.tickers_failed}[/{color}]"
    )


# Re-export bound logger so tests / users can discover it easily
log = get_logger("stonks.cli")


if __name__ == "__main__":
    app()

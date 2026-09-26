"""``stonks universe ...``: stored universes from the terminal (roadmap 10.5).

``app`` is a Typer app that the main CLI mounts as ``stonks universe``.
Every command opens the lake itself, like the other CLI commands, so none
of them may run while ``stonks serve`` holds the lake: use the API or the
console then (``/api/universes``).
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from stonks.core.interval import Interval
from stonks.ingest.sources.registry import (
    DEFAULT_SOURCE_ID,
    SOURCE_IDS,
    SourceConfigError,
    build_source,
)

app = typer.Typer(help="Stored universes and on-demand data", no_args_is_help=True)
console = Console()

_KINDS = ("list", "exchange", "rule", "index")
_CSV = typer.Option(
    None, "--csv", help="list only: CSV with a ticker column and optional start/end dates"
)
_HISTORY = typer.Argument(..., help="CSV (date,ticker,action) or JSON history")


def _settings() -> Any:
    from stonks.cli import _settings as cli_settings

    return cli_settings()


def _lake(settings: Any) -> Any:
    from stonks.store.lake import DuckDBLake

    lake = DuckDBLake(settings.lake.path)
    lake.migrate()
    return lake


def _date(value: str | None, flag: str) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise typer.BadParameter(f"{flag} must be YYYY-MM-DD, got {value!r}") from None


def _fail(message: str) -> None:
    console.print(f"[red]{message}[/red]")
    raise typer.Exit(code=1)


def ensure_source(settings: Any, source_id: str) -> Any:
    """The data source an ensure fetches from (``--source``)."""
    if source_id not in SOURCE_IDS:
        raise typer.BadParameter(f"--source must be one of {list(SOURCE_IDS)}, got {source_id!r}")
    try:
        return build_source(source_id, settings.sources)
    except SourceConfigError as exc:
        raise typer.BadParameter(str(exc)) from None


def _print_ensure(report: Any) -> None:
    console.print(
        f"{report.tickers_requested} requested, {report.tickers_up_to_date} up to date, "
        f"{report.tickers_fetched} fetched, {report.tickers_failed} failed "
        f"({report.start} to {report.end}, {report.interval}, {report.source})"
    )
    for warning in report.warnings:
        console.print(f"[yellow]warning[/yellow] {warning}")
    if report.failed:
        console.print(f"[yellow]failed[/yellow] {', '.join(report.failed)}")


@app.command("list")
def list_universes() -> None:
    """Every stored universe with its kind, member count and last refresh."""
    from stonks.universes import UniverseStore

    settings = _settings()
    with _lake(settings) as lake:
        definitions = UniverseStore(lake).list()
    table = Table(title=f"{len(definitions)} universes")
    for col in ("id", "kind", "name", "members", "refreshed"):
        table.add_column(col)
    for d in definitions:
        table.add_row(
            d.id,
            d.kind,
            d.name or "",
            "-" if d.member_count is None else str(d.member_count),
            "never" if d.refreshed_at is None else d.refreshed_at.isoformat(timespec="minutes"),
        )
    console.print(table)


@app.command("show")
def show(universe_id: str = typer.Argument(..., help="universe id")) -> None:
    """A universe's definition as JSON."""
    from stonks.universes import UniverseStore

    settings = _settings()
    with _lake(settings) as lake:
        try:
            definition = UniverseStore(lake).get(universe_id)
        except KeyError:
            _fail(f"no universe {universe_id!r}")
    console.print_json(definition.model_dump_json())


@app.command("create")
def create(
    universe_id: str = typer.Argument(..., help="new universe id (lowercase, digits, _ . -)"),
    kind: str = typer.Option("list", "--kind", help="|".join(_KINDS)),
    tickers: str | None = typer.Option(
        None, "--tickers", help="list only: comma-separated tickers"
    ),
    csv: Path | None = _CSV,
    spec: str | None = typer.Option(
        None, "--spec", help="kind settings as a JSON object (see docs/universes.md)"
    ),
    name: str | None = typer.Option(None, "--name"),
    description: str | None = typer.Option(None, "--description"),
) -> None:
    """Store a universe definition. It has no members until its first refresh."""
    from stonks.universes import UniverseDefinition, UniverseStore
    from stonks.universes.providers.static_list import parse_list_csv

    if kind not in _KINDS:
        raise typer.BadParameter(f"--kind must be one of {list(_KINDS)}, got {kind!r}")
    if (tickers or csv) and kind != "list":
        raise typer.BadParameter("--tickers and --csv are for list universes")
    try:
        body: dict[str, Any] = json.loads(spec) if spec else {}
    except json.JSONDecodeError as exc:
        raise typer.BadParameter(f"--spec is not JSON: {exc}") from None
    if not isinstance(body, dict):
        raise typer.BadParameter("--spec must be a JSON object")
    try:
        if csv is not None:
            body = parse_list_csv(csv.read_text(encoding="utf-8"))
        elif tickers:
            body["tickers"] = [t.strip() for t in tickers.split(",") if t.strip()]
        definition = UniverseDefinition(
            id=universe_id, kind=kind, name=name, description=description, spec=body
        )
        definition.validated()
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from None
    settings = _settings()
    with _lake(settings) as lake:
        store = UniverseStore(lake)
        if store.exists(universe_id):
            _fail(f"universe {universe_id!r} already exists")
        store.save(definition)
    console.print(f"[green]created {universe_id}[/green] ({kind}); refresh it to add members")


@app.command("delete")
def delete(
    universe_id: str = typer.Argument(..., help="universe id"),
    yes: bool = typer.Option(False, "--yes", help="confirm: lab runs and ticks may name it"),
) -> None:
    """Delete a definition and its membership rows."""
    from stonks.universes import UniverseStore

    if not yes:
        _fail(f"deleting {universe_id!r} can break lab runs and ticks that name it: add --yes")
    settings = _settings()
    with _lake(settings) as lake:
        store = UniverseStore(lake)
        if not store.exists(universe_id):
            _fail(f"no universe {universe_id!r}")
        store.delete(universe_id)
    console.print(f"deleted {universe_id}")


@app.command("refresh")
def refresh(
    universe_id: str = typer.Argument(..., help="universe id"),
    as_of: str | None = typer.Option(None, "--as-of", help="refresh date (default today)"),
) -> None:
    """Rebuild the universe's point-in-time membership from its definition."""
    from stonks.universes import UniverseStore, refresh_universe

    day = _date(as_of, "--as-of")
    settings = _settings()
    with _lake(settings) as lake:
        if not UniverseStore(lake).exists(universe_id):
            _fail(f"no universe {universe_id!r}")
        try:
            result = refresh_universe(
                lake,
                universe_id,
                as_of=day,
                source_factory=lambda sid: ensure_source(settings, sid or DEFAULT_SOURCE_ID),
            )
        except ValueError as exc:
            _fail(str(exc))
    console.print(
        f"{universe_id}: {result.members} members, {result.current_members} current, "
        f"{result.spans} spans"
    )
    for warning in result.warnings:
        console.print(f"[yellow]warning[/yellow] {warning}")


@app.command("members")
def members(
    universe_id: str = typer.Argument(..., help="universe id"),
    as_of: str | None = typer.Option(None, "--as-of", help="YYYY-MM-DD (default today)"),
) -> None:
    """Members on one day, one per line."""
    day = _date(as_of, "--as-of") or datetime.now(UTC).date()
    settings = _settings()
    with _lake(settings) as lake:
        if universe_id not in lake.universe_ids():
            _fail(f"universe {universe_id!r} has no members: create and refresh it first")
        tickers = lake.members_as_of(universe_id, day)
    for ticker in tickers:
        console.print(ticker)


@app.command("ensure")
def ensure(
    universe_id: str = typer.Argument(..., help="universe id"),
    start: str = typer.Option(..., "--start", help="YYYY-MM-DD"),
    end: str = typer.Option(..., "--end", help="YYYY-MM-DD"),
    interval: str = typer.Option("1d", "--interval", help="bar interval (1d, 1h, 5m, ...)"),
    source: str = typer.Option(DEFAULT_SOURCE_ID, "--source", help="|".join(SOURCE_IDS)),
) -> None:
    """Fetch the missing bars of every member over the window, delisted
    names included. Only the gaps are fetched ([ensure] settings)."""
    from stonks.app.lab import build_data_ensurer

    start_d, end_d = _date(start, "--start"), _date(end, "--end")
    assert start_d is not None and end_d is not None
    if start_d > end_d:
        raise typer.BadParameter("--start must be on or before --end")
    try:
        bar_interval = Interval.parse(interval)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--interval") from None
    settings = _settings()
    data_source = ensure_source(settings, source)
    with _lake(settings) as lake:
        if universe_id not in lake.universe_ids():
            _fail(f"universe {universe_id!r} has no members: create and refresh it first")
        tickers = lake.members_between(universe_id, start_d, end_d)
        report = build_data_ensurer(settings, lake, data_source).ensure(
            tickers, start_d, end_d, bar_interval
        )
    _print_ensure(report)


@app.command("import-index")
def import_index(
    index_id: str = typer.Argument(..., help="index id, e.g. sp500"),
    path: Path = _HISTORY,
    fmt: str | None = typer.Option(None, "--format", help="csv|json (default: file extension)"),
) -> None:
    """Import an index's constituents and changes for index universes."""
    from stonks.universes import UniverseStore
    from stonks.universes.index_import import parse_index_history

    kind = fmt or ("json" if path.suffix.lower() == ".json" else "csv")
    if kind not in ("csv", "json"):
        raise typer.BadParameter("--format must be csv or json")
    try:
        history = parse_index_history(path.read_text(encoding="utf-8"), kind, index_id=index_id)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from None
    settings = _settings()
    with _lake(settings) as lake:
        UniverseStore(lake).save_index_history(history)
    console.print(
        f"{index_id}: {len(history.constituents)} constituents, {len(history.changes)} changes"
    )

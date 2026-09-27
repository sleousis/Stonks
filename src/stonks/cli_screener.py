"""``stonks screener``: run screens, keep saved ones and store a screen as a
universe (roadmap 20.8). Mounted by :mod:`stonks.cli`.

Commands act as the owner unless ``--user`` names someone. They open the
lake, so while ``stonks serve`` runs, use the API, the console or MCP."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from stonks.cli_calendars import call, cli_principal

app = typer.Typer(help="Stock screener and saved screens", no_args_is_help=True)

_USER = typer.Option(None, "--user", help="act as this user (email or id); default: the owner")
_SPEC = typer.Option(
    None, "--spec", help='screen as JSON, e.g. {"filters": [{"metric": "pe_ratio", "max": 15}]}'
)
_SCREEN = typer.Option(None, "--screen", help="one of your saved screens by id")


def _day(value: str | None, flag: str) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise typer.BadParameter("expected YYYY-MM-DD", param_hint=flag) from None


def _spec(value: str | None) -> dict[str, Any] | None:
    if value is None:
        return None
    try:
        body = json.loads(value)
    except json.JSONDecodeError as exc:
        raise typer.BadParameter(f"not JSON: {exc}", param_hint="--spec") from None
    if not isinstance(body, dict):
        raise typer.BadParameter("must be a JSON object", param_hint="--spec")
    return body


@contextmanager
def _service() -> Iterator[tuple[Any, Any, Any]]:
    from stonks.app.context import AppContext
    from stonks.app.jobs import JobRunner, JobStore
    from stonks.app.screener import ScreenerService
    from stonks.app.universes import UniverseService
    from stonks.cli import _settings

    settings = _settings()
    context = AppContext(settings)
    with context.state() as state:
        state.migrate()
    with context.lake() as lake:
        lake.migrate()
    runner = JobRunner(JobStore(settings.state.path), max_workers=1)
    try:
        universes = UniverseService(context, runner)
        yield context, ScreenerService(context, universes), universes
    finally:
        runner.shutdown()


def _fmt(value: float | None) -> str:
    return "-" if value is None else f"{value:.4g}"


@app.command("metrics")
def metrics() -> None:
    """Every metric a screen may filter or sort on."""
    from stonks.app.screener import ScreenerService

    table = Table(title="screen metrics")
    for col in ("id", "group", "unit", "description"):
        table.add_column(col)
    for m in ScreenerService.metrics():
        table.add_row(m.id, m.group, m.unit, m.description)
    Console().print(table)


@app.command("run")
def run(
    spec: str | None = _SPEC,
    screen: str | None = _SCREEN,
    as_of: str | None = typer.Option(None, "--as-of", help="screen on this day (default today)"),
    user: str | None = _USER,
) -> None:
    """Run a screen and print the matches with their metric values."""
    from stonks.app.screener import ScreenRunRequest

    body = {"spec": _spec(spec), "screen_id": screen, "as_of": _day(as_of, "--as-of")}
    request = call(lambda: ScreenRunRequest.model_validate(body))
    with _service() as (context, service, _):
        result = call(lambda: service.run(cli_principal(context, user), request))
    table = Table(
        title=f"{result.matched} of {result.candidates} candidates on {result.as_of}"
        + (" (cut to the limit)" if result.truncated else "")
    )
    for col in ("ticker", "name", "sector", *result.metrics):
        table.add_column(col)
    for row in result.rows:
        table.add_row(
            row.ticker,
            row.name or "",
            row.sector or "",
            *(_fmt(row.values.get(m)) for m in result.metrics),
        )
    Console().print(table)


@app.command("list")
def list_screens(user: str | None = _USER) -> None:
    """Your saved screens."""
    with _service() as (context, service, _):
        screens = call(lambda: service.list(cli_principal(context, user)))
    if not screens:
        Console().print("no saved screens")
        return
    table = Table(title="saved screens")
    for col in ("id", "name", "spec"):
        table.add_column(col)
    for s in screens:
        table.add_row(s.id, s.name, json.dumps(s.spec.model_dump(exclude_defaults=True)))
    Console().print(table)


@app.command("save")
def save(
    name: str = typer.Argument(..., help="a name, unique among your screens"),
    spec: str = typer.Option(..., "--spec", help="screen as JSON"),
    user: str | None = _USER,
) -> None:
    """Save a screen under a name."""
    from stonks.app.screener import SavedScreenCreate

    body = call(lambda: SavedScreenCreate.model_validate({"name": name, "spec": _spec(spec)}))
    with _service() as (context, service, _):
        saved = call(lambda: service.create(cli_principal(context, user), body))
    Console().print(f"saved {saved.id} ({saved.name})")


@app.command("delete")
def delete(screen_id: str = typer.Argument(...), user: str | None = _USER) -> None:
    """Delete one of your saved screens."""
    with _service() as (context, service, _):
        call(lambda: service.delete(cli_principal(context, user), screen_id))
    Console().print(f"deleted {screen_id}")


@app.command("universe")
def universe(
    universe_id: str = typer.Argument(..., help="new universe id (lowercase, digits, _ . -)"),
    spec: str | None = _SPEC,
    screen: str | None = _SCREEN,
    mode: str = typer.Option("rule", "--mode", help="rule (point in time) | snapshot"),
    start: str | None = typer.Option(None, "--start", help="rule: first rebalance date"),
    end: str | None = typer.Option(None, "--end", help="rule: last date (default open)"),
    rebalance: str = typer.Option("monthly", "--rebalance", help="weekly | monthly | quarterly"),
    name: str | None = typer.Option(None, "--name"),
    refresh: bool = typer.Option(True, "--refresh/--no-refresh", help="fill the members now"),
    user: str | None = _USER,
) -> None:
    """Store a screen as a universe for the lab, then fill its members."""
    from stonks.app.screener import ScreenUniverseRequest

    body = {
        "universe_id": universe_id,
        "spec": _spec(spec),
        "screen_id": screen,
        "mode": mode,
        "start": _day(start, "--start"),
        "end": _day(end, "--end"),
        "rebalance": rebalance,
        "name": name,
        "refresh": False,
    }
    request = call(lambda: ScreenUniverseRequest.model_validate(body))
    with _service() as (context, service, universes):
        view = call(lambda: service.to_universe(cli_principal(context, user), request))
        for warning in view.warnings:
            Console().print(f"[yellow]warning[/yellow]: {warning}")
        Console().print(f"stored {view.universe.kind} universe {view.universe.id}")
        if refresh:
            done = call(lambda: universes.refresh(universe_id))
            Console().print(f"{done.members} members, {done.current_members} today")

"""``stonks starter``: the starter set (complexity audit F19). ``install``
registers three simple strategies On trial and, when none is configured,
a small trading universe. ``stonks users bootstrap`` runs it too."""

from __future__ import annotations

from typing import Any

import typer

app = typer.Typer(help="The starter strategies for a fresh install", no_args_is_help=True)


def _service(settings: Any) -> Any:
    from stonks.app.context import AppContext
    from stonks.app.starter import StarterService

    context = AppContext(settings)
    with context.state() as state:
        state.migrate()
    return StarterService(context)


def install_and_print(settings: Any) -> None:
    from stonks.accounts import Scope

    done = _service(settings).install(Scope.service("cli"))
    for sid in done.registered:
        typer.echo(f"on trial: {sid}")
    if done.skipped:
        typer.echo(f"already registered: {', '.join(done.skipped)}")
    if done.universe:
        typer.echo(f"trading universe: {', '.join(done.universe)}")
    for step in done.next_steps:
        typer.echo(f"next: {step}")


@app.command("install")
def install() -> None:
    """Put the starter strategies On trial (never approved)."""
    from stonks.cli import _settings

    install_and_print(_settings())


@app.command("list")
def list_starters() -> None:
    """The starter strategies and whether each is registered."""
    from stonks.cli import _settings
    from stonks.starter import STARTERS
    from stonks.store.state import SqliteState

    settings = _settings()
    with SqliteState(settings.state.path) as state:
        state.migrate()
        found = {r["id"]: r["status"] for r in state.sql("SELECT id, status FROM strategies")}
    for spec in STARTERS:
        typer.echo(f"{spec.id}: {found.get(spec.id, 'not installed')}  {spec.title}")

"""``stonks price-alerts``: a person's price alert rules, and the check the
scheduler runs after each price ingest.

Mounted by :mod:`stonks.cli`. Rules belong to a person, so ``list``,
``create``, ``delete`` and ``events`` name one with ``--user``. ``run``
checks every rule of every person, as the operator."""

from __future__ import annotations

from datetime import date
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

app = typer.Typer(help="Price alerts on tickers and watchlists", no_args_is_help=True)

_USER = typer.Option(..., "--user", help="the person (email or id)")


class _LazyConsole:
    def print(self, *args: Any, **kwargs: Any) -> None:
        Console().print(*args, **kwargs)


console = _LazyConsole()


def _service() -> tuple[Any, Any]:
    from stonks.app.context import AppContext
    from stonks.app.price_alerts import PriceAlertService
    from stonks.cli import _settings

    context = AppContext(_settings())
    with context.state() as state:
        state.migrate()
    return context, PriceAlertService(context)


def _principal(context: Any, user: str) -> Any:
    """The person as a principal acting from the shell (their role's scopes)."""
    from stonks.accounts import NotFound, UserRepository
    from stonks.auth.principal import ROLE_SCOPES, Principal

    with context.state() as state:
        users = UserRepository(state)
        try:
            found = users.get_by_email(user) if "@" in user else users.get(user)
        except NotFound:
            raise typer.BadParameter(f"no user {user!r}", param_hint="--user") from None
    return Principal.create(
        user_id=found.id,
        kind=found.kind,
        role=found.role,
        scopes=ROLE_SCOPES[found.role],
        mfa_fresh=False,
        via="cli",
    )


def _call(fn: Any) -> Any:
    from stonks.app.errors import AppError
    from stonks.auth.errors import PermissionDenied

    try:
        return fn()
    except (AppError, PermissionDenied, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from None


def _target(rule: Any) -> str:
    return rule.ticker or f"watchlist {rule.watchlist_id}"


def _threshold(rule: Any) -> str:
    if rule.condition == "moves_pct":
        return f"{rule.pct:g}% in {rule.window_days}d"
    return f"{rule.level:g}"


@app.command("list")
def list_rules(user: str = _USER) -> None:
    """A person's rules, with the price each last saw."""
    context, service = _service()
    rules = _call(lambda: service.list(_principal(context, user)))
    if not rules:
        console.print("no price alerts")
        return
    table = Table(title="price alerts")
    for col in ("id", "name", "target", "condition", "threshold", "on", "last seen"):
        table.add_column(col)
    for r in rules:
        seen = ", ".join(f"{t} {v['price']:g} ({v['observed_at']})" for t, v in r.last_seen.items())
        table.add_row(
            r.id,
            r.name or "",
            _target(r),
            r.condition,
            _threshold(r),
            "yes" if r.enabled else "no",
            seen or "-",
        )
    console.print(table)


@app.command("create")
def create(
    condition: str = typer.Option(
        ..., "--condition", help="crosses_above | crosses_below | moves_pct"
    ),
    ticker: str | None = typer.Option(None, "--ticker", help="one instrument id"),
    watchlist: str | None = typer.Option(None, "--watchlist", help="or one of the watchlist ids"),
    level: float | None = typer.Option(None, "--level", help="the price (crossings)"),
    pct: float | None = typer.Option(None, "--pct", help="percent move (moves_pct)"),
    window_days: int | None = typer.Option(None, "--window-days", help="days (moves_pct)"),
    name: str | None = typer.Option(None, "--name"),
    user: str = _USER,
) -> None:
    """Create a rule on a ticker or one of the person's watchlists."""
    from stonks.app.price_alerts import PriceAlertCreate

    context, service = _service()
    principal = _principal(context, user)
    try:
        body = PriceAlertCreate(
            condition=condition,  # type: ignore[arg-type]
            ticker=ticker,
            watchlist_id=watchlist,
            level=level,
            pct=pct,
            window_days=window_days,
            name=name,
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from None
    rule = _call(lambda: service.create(principal, body))
    console.print(f"[green]created[/green] {rule.id}: {_target(rule)} {rule.condition}")


@app.command("delete")
def delete(alert_id: str = typer.Argument(...), user: str = _USER) -> None:
    """Delete one of the person's rules."""
    context, service = _service()
    principal = _principal(context, user)
    _call(lambda: service.delete(principal, alert_id))
    console.print(f"deleted {alert_id}")


@app.command("events")
def events(user: str = _USER, limit: int = typer.Option(20, "--limit", min=1, max=500)) -> None:
    """When the person's rules fired, newest first."""
    context, service = _service()
    principal = _principal(context, user)
    page = _call(lambda: service.events(principal, rule_id=None, limit=limit, offset=0))
    if not page.items:
        console.print("no price alert has fired yet")
        return
    for e in page.items:
        console.print(f"{e.observed_at} {e.ticker} {e.price:g}: {e.detail} ({e.rule_id})")


@app.command("run")
def run(
    as_of: str | None = typer.Option(None, "--as-of", help="YYYY-MM-DD; default today"),
) -> None:
    """Check every enabled rule of every person against the latest closes
    and send what fired (the scheduler's price_alerts job)."""
    try:
        day = date.fromisoformat(as_of) if as_of else None
    except ValueError:
        raise typer.BadParameter("expected YYYY-MM-DD", param_hint="--as-of") from None
    _, service = _service()
    out = _call(lambda: service.evaluate(None, day))
    console.print(
        f"{out.rules} rules, {out.checked} checked, {out.fired} fired, {out.published} sent"
    )

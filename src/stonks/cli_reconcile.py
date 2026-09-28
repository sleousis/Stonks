"""``stonks reconcile``: reconciliation of live portfolios against their
broker (roadmap 19.5).

Mounted by :mod:`stonks.cli`. The shell is the operator, so every
portfolio's reports are visible. ``run`` checks a portfolio listed on an IB
Gateway by hand (an ``adhoc`` report unless ``--kind`` says otherwise) and
acts on it like the scheduled checks: drift opens the ``broker_drift``
halt and pauses auto.
"""

from __future__ import annotations

import json
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from stonks.execution.drift import DriftItem

app = typer.Typer(help="Reconcile live portfolios against their broker", no_args_is_help=True)

_STATUS_STYLE = {
    "clean": "green",
    "warn": "yellow",
    "drift": "red",
    "outage": "yellow",
    "fault": "red",
}


class _LazyConsole:
    def print(self, *args: Any, **kwargs: Any) -> None:
        Console().print(*args, **kwargs)


console = _LazyConsole()


def _state() -> Any:
    from stonks.cli import _settings
    from stonks.store.state import SqliteState

    settings = _settings()
    state = SqliteState(settings.state.path)
    state.migrate()
    return settings, state


def _styled(status: str) -> str:
    style = _STATUS_STYLE.get(status, "white")
    return f"[{style}]{status}[/{style}]"


@app.command("list")
def list_cmd(
    portfolio: str | None = typer.Option(None, "--portfolio", help="one portfolio id"),
    limit: int = typer.Option(20, "--limit", min=1, max=500),
) -> None:
    """The latest reconcile reports, newest first."""
    from stonks.production.live.checks import list_reports

    _, state = _state()
    try:
        reports = list_reports(state, portfolio_ids=[portfolio] if portfolio else None, limit=limit)
    finally:
        state.close()
    if not reports:
        console.print("no reconcile reports")
        return
    table = Table(title="reconcile reports")
    table.add_column("id", no_wrap=True)
    for col in ("portfolio", "kind", "day", "status", "items", "halt", "paused"):
        table.add_column(col)
    for r in reports:
        table.add_row(
            r.id,
            r.portfolio_id,
            r.kind,
            r.as_of.isoformat(),
            _styled(r.status),
            str(len(r.items)),
            f"#{r.halt_id}" if r.halt_id else "-",
            str(len(r.paused)),
        )
    console.print(table)


def _items_table(title: str, items: tuple[DriftItem, ...]) -> Table:
    table = Table(title=title)
    for col in ("kind", "key", "ours", "broker", "material", "detail"):
        table.add_column(col)
    for i in items:
        table.add_row(
            i.kind,
            i.key,
            "-" if i.ours is None else str(i.ours),
            "-" if i.broker is None else str(i.broker),
            "yes" if i.material else "no",
            i.detail,
        )
    return table


@app.command("show")
def show_cmd(report_id: str = typer.Argument(..., help="a report id (rec_...)")) -> None:
    """One report: every unexplained item, what the check explained itself,
    and the owner's own positions and orders it kept apart."""
    from stonks.production.live.checks import get_report

    _, state = _state()
    try:
        report = get_report(state, report_id)
    finally:
        state.close()
    if report is None:
        console.print(f"[red]error[/red]: no reconcile report {report_id!r}")
        raise typer.Exit(code=1)
    console.print(
        f"{report.id}: {report.portfolio_id} {report.kind} check on {report.as_of} "
        f"at {report.taken_at}: {_styled(report.status)}"
    )
    if report.detail:
        console.print(f"broker: {report.detail}")
    if report.halt_id:
        console.print(f"broker_drift halt: #{report.halt_id}")
    if report.paused:
        console.print(f"auto paused: {', '.join(report.paused)}")
    if report.items:
        console.print(_items_table("unexplained", report.items))
    if report.explained:
        console.print(_items_table("explained", report.explained))
    external = dict(report.external)
    if external.get("positions") or external.get("orders"):
        console.print(f"external (the owner's own): {json.dumps(external, sort_keys=True)}")
    console.print(f"reconciliation: {json.dumps(dict(report.summary), sort_keys=True)}")


@app.command("run")
def run_cmd(
    portfolio: str = typer.Option(..., "--portfolio", help="a portfolio listed on a gateway"),
    kind: str = typer.Option("adhoc", "--kind", help="adhoc, sod, submit or eod"),
) -> None:
    """Check one live portfolio against its IB Gateway now. Exit code 1 on
    drift, an outage or a fault."""
    from stonks.production.live.checks import check_kind, run_gateway_checks

    try:
        which = check_kind(kind)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--kind") from None
    settings, state = _state()
    try:
        config = settings.brokers.ibkr
        if not any(portfolio in gw.portfolios for gw in config.gateways.values()):
            console.print(
                f"[red]error[/red]: no IB Gateway lists portfolio {portfolio!r} "
                "([brokers.ibkr.gateways.<name>] portfolios)"
            )
            raise typer.Exit(code=1)
        [result] = run_gateway_checks(
            state,
            config,
            which,
            settings=settings.production.live,
            portfolio_ids=[portfolio],
            settlement=settings.production.risk.rules.account_rules,
        )
    finally:
        state.close()
    report = result.report
    console.print(f"{report.id}: {_styled(report.status)} ({len(report.items)} item(s))")
    if report.items:
        console.print(_items_table("unexplained", report.items))
    if report.detail:
        console.print(f"broker: {report.detail}")
    if report.halt_id:
        console.print(f"broker_drift halt: #{report.halt_id}")
    if report.paused:
        console.print(f"auto paused: {', '.join(report.paused)}")
    if report.status in ("drift", "outage", "fault"):
        raise typer.Exit(code=1)

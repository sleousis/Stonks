"""``stonks cash-flows``: deposits and withdrawals of a portfolio.

Mounted by :mod:`stonks.cli`. Runs as the operator (``service:cli``, any
portfolio, ``--portfolio`` required) unless ``--user`` names a user, who
then acts on their own portfolios only."""

from __future__ import annotations

from datetime import date
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

app = typer.Typer(help="Deposits and withdrawals of a portfolio", no_args_is_help=True)

_USER = typer.Option(
    None, "--user", help="act as this user (email or id); default: the operator (service:cli)"
)
_PORTFOLIO = typer.Option(
    None, "--portfolio", help="portfolio id; default: the default book (or the user's own)"
)


def _service() -> tuple[Any, Any]:
    from stonks.app.cash_flows import CashFlowService
    from stonks.app.context import AppContext
    from stonks.cli import _settings

    context = AppContext(_settings())
    with context.state() as state:
        state.migrate()
    return context, CashFlowService(context)


def _call(fn: Any) -> Any:
    from stonks.app.errors import AppError
    from stonks.auth.errors import PermissionDenied

    try:
        return fn()
    except (AppError, PermissionDenied, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from None


def _target(context: Any, user: str | None, portfolio: str | None) -> tuple[Any, str]:
    from stonks.cli import _halt_scope
    from stonks.cli_tca import _portfolio

    scope = _halt_scope(context, user)
    return scope, _portfolio(context, scope, portfolio)


@app.command("record")
def record(
    kind: str = typer.Option(..., "--kind", help="deposit | withdrawal"),
    amount: float = typer.Option(..., "--amount", help="a positive amount"),
    on: str | None = typer.Option(None, "--date", help="YYYY-MM-DD; default today"),
    note: str | None = typer.Option(None, "--note"),
    portfolio: str | None = _PORTFOLIO,
    user: str | None = _USER,
) -> None:
    """Record a deposit or a withdrawal. It moves the simulated book's cash."""
    from stonks.app.cash_flows import CashFlowCreate

    context, service = _service()
    scope, pid = _target(context, user, portfolio)
    try:
        body = CashFlowCreate(
            kind=kind,  # type: ignore[arg-type]
            amount=amount,
            flow_date=date.fromisoformat(on) if on else None,
            note=note,
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from None
    view = _call(lambda: service.record(scope, pid, body))
    Console().print(f"recorded {view.kind} of {view.amount:,.2f} on {view.flow_date} ({pid})")


@app.command("list")
def list_flows(portfolio: str | None = _PORTFOLIO, user: str | None = _USER) -> None:
    """Every deposit and withdrawal of a portfolio, oldest first."""
    context, service = _service()
    scope, pid = _target(context, user, portfolio)
    flows = _call(lambda: service.list(scope, pid))
    if not flows:
        Console().print("no deposits or withdrawals")
        return
    table = Table(title=f"cash flows ({pid})")
    for col in ("date", "kind", "amount", "source", "note"):
        table.add_column(col)
    for f in flows:
        table.add_row(f.flow_date.isoformat(), f.kind, f"{f.amount:,.2f}", f.source, f.note or "")
    Console().print(table)

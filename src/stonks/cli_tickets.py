"""``stonks tickets``: list, show, approve and reject order tickets
(roadmap 19.8).

Mounted by :mod:`stonks.cli`. Every command runs as the operator
(``service:cli``, every portfolio) unless ``--user`` names a user, who
then sees and decides only their own tickets, within their role. Approving
asks you to type ``APPROVE TICKETS`` at a terminal: the shell's stand-in
for the fresh second factor the web app asks for. A script or a pipe
cannot approve."""

from __future__ import annotations

import sys
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

app = typer.Typer(
    help="Order tickets: what live books decided, approve or reject", no_args_is_help=True
)


class _LazyConsole:
    def print(self, *args: Any, **kwargs: Any) -> None:
        Console().print(*args, **kwargs)


console = _LazyConsole()

_USER = typer.Option(
    None, "--user", help="act as this user (email or id); default: the operator (service:cli)"
)
_TICKET_IDS = typer.Argument(..., help="ticket ids, approved all or none")
_STATUSES = (
    "awaiting_approval",
    "approved",
    "rejected",
    "expired",
    "submitted",
    "filled",
    "unfilled",
    "cancelled",
    "failed",
)


def _interactive() -> bool:
    """True when a person sits at the terminal (stdin is a tty)."""
    return sys.stdin.isatty()


def _service() -> tuple[Any, Any]:
    from stonks.app.context import AppContext
    from stonks.app.tickets import TicketService
    from stonks.cli import _settings

    context = AppContext(_settings())
    with context.state() as state:
        state.migrate()
    return context, TicketService(context)


def _who(context: Any, user: str | None) -> Any:
    from stonks.cli import _halt_scope

    return _halt_scope(context, user)


def _call(fn: Any) -> Any:
    """Run a service call, turning its refusals into a usage error."""
    from stonks.app.errors import AppError
    from stonks.auth.errors import PermissionDenied

    try:
        return fn()
    except (AppError, PermissionDenied) as exc:
        raise typer.BadParameter(str(exc)) from None
    except ValueError as exc:  # request model validation
        raise typer.BadParameter(str(exc)) from None


def _money(value: float | None) -> str:
    return "-" if value is None else f"{value:,.2f}"


@app.command("list")
def list_(
    status: str | None = typer.Option(
        None, "--status", help=f"only this status: {' | '.join(_STATUSES)}"
    ),
    portfolio: str | None = typer.Option(None, "--portfolio", help="only this portfolio id"),
    user: str | None = _USER,
) -> None:
    """Order tickets, newest first."""
    if status is not None and status not in _STATUSES:
        raise typer.BadParameter(" | ".join(_STATUSES), param_hint="--status")
    context, service = _service()
    who = _who(context, user)
    views = _call(lambda: service.list(who, status=status, portfolio_id=portfolio))
    if not views:
        console.print("no tickets")
        return
    table = Table(title="order tickets")
    for col in ("id", "portfolio", "as of", "order", "about", "status", "send by"):
        table.add_column(col)
    for t in views:
        table.add_row(
            t.id,
            t.portfolio_name,
            t.as_of.isoformat(),
            f"{t.side} {t.quantity:g} {t.ticker}",
            _money(t.notional),
            t.status,
            t.expires_at.isoformat(),
        )
    console.print(table)


@app.command("show")
def show(
    ticket_id: str = typer.Argument(..., help="the ticket id (tkt_...)"),
    user: str | None = _USER,
) -> None:
    """One ticket: the order, why it waits, the rules that touched it."""
    context, service = _service()
    who = _who(context, user)
    t = _call(lambda: service.get(who, ticket_id))
    price = f"limit {t.limit_price:g}" if t.limit_price is not None else "at the open"
    console.print(f"[bold]{t.id}[/bold] {t.side} {t.quantity:g} {t.ticker} ({price})")
    console.print(f"  portfolio   {t.portfolio_name} ({t.portfolio_id})")
    console.print(f"  decided     {t.as_of.isoformat()} at {_money(t.reference_price)}")
    console.print(f"  about       {_money(t.notional)}")
    console.print(f"  strategy    {t.strategy_id or '-'}")
    console.print(
        f"  status      {t.status}" + (f" ({t.status_reason})" if t.status_reason else "")
    )
    console.print(f"  waits for   {t.hold or '-'}")
    console.print(f"  send window {t.submit_after.isoformat()} to {t.expires_at.isoformat()}")
    if t.decided_by:
        why = f": {t.decision_reason}" if t.decision_reason else ""
        console.print(f"  decided by  {t.decided_by}{why}")
    for rule in t.rules:
        console.print(f"  rule        {rule.get('rule', '?')}: {rule.get('reason', '')}")


@app.command("approve")
def approve(
    ticket_ids: list[str] = _TICKET_IDS,
    user: str | None = _USER,
) -> None:
    """Approve tickets that wait (all or none). Asks you to type APPROVE
    TICKETS at a terminal. Approved tickets go out in their submit window."""
    from stonks.app.tickets import APPROVE_PHRASE, TicketApproval

    if not _interactive():
        raise typer.BadParameter(
            f"approving needs {APPROVE_PHRASE} typed at a terminal; nothing was approved"
        )
    context, service = _service()
    who = _who(context, user)
    body = _call(lambda: TicketApproval(ticket_ids=ticket_ids))
    typed = typer.prompt(f"Type {APPROVE_PHRASE} to approve {len(ticket_ids)} ticket(s)")
    done = _call(lambda: service.approve(who, body, confirmation=typed))
    console.print(f"[green]approved {len(done.items)}[/green]: sent in their submit window")
    for t in done.items:
        console.print(f"  {t.id} {t.side} {t.quantity:g} {t.ticker} by {t.expires_at.isoformat()}")


@app.command("reject")
def reject(
    ticket_id: str = typer.Argument(..., help="the ticket id (tkt_...)"),
    reason: str = typer.Option(..., "--reason", help="why (kept with the ticket, audited)"),
    user: str | None = _USER,
) -> None:
    """Reject a ticket that waits, with a reason. Nothing is sent."""
    from stonks.app.tickets import TicketRejection

    context, service = _service()
    who = _who(context, user)
    body = _call(lambda: TicketRejection(reason=reason))
    t = _call(lambda: service.reject(who, ticket_id, body))
    console.print(f"[yellow]rejected[/yellow] {t.id} {t.side} {t.quantity:g} {t.ticker}")

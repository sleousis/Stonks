"""``stonks tca``: transaction cost analysis and the trade journal (BL-32).

Mounted by :mod:`stonks.cli`. Every command runs as the operator
(``service:cli``, every book) unless ``--user`` names a user, who then only
sees their own portfolios."""

from __future__ import annotations

from datetime import date
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from stonks.production.tca import GROUP_BYS

app = typer.Typer(
    help="Transaction cost analysis and the trade journal (BL-32)", no_args_is_help=True
)


class _LazyConsole:
    """A fresh rich console per call, so the terminal width is read when a
    command prints, not at import."""

    def print(self, *args: Any, **kwargs: Any) -> None:
        Console().print(*args, **kwargs)


console = _LazyConsole()

_USER = typer.Option(
    None, "--user", help="act as this user (email or id); default: the operator (service:cli)"
)
_PORTFOLIO = typer.Option(
    None, "--portfolio", help="portfolio id; default: the default book (or the user's own)"
)
_SINCE = typer.Option(None, "--since", help="orders decided on or after this day (YYYY-MM-DD)")


def _day(value: str | None, flag: str) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise typer.BadParameter("expected YYYY-MM-DD", param_hint=flag) from None


def _service() -> tuple[Any, Any]:
    from stonks.app.context import AppContext
    from stonks.app.tca import TcaService
    from stonks.cli import _settings

    context = AppContext(_settings())
    with context.state() as state:
        state.migrate()
    return context, TcaService(context)


def _call(fn: Any) -> Any:
    from stonks.app.errors import AppError

    try:
        return fn()
    except AppError as exc:
        raise typer.BadParameter(str(exc)) from None


def _who(context: Any, user: str | None) -> Any:
    from stonks.cli import _halt_scope

    return _halt_scope(context, user)


def _portfolio(context: Any, scope: Any, portfolio: str | None) -> str:
    """The portfolio a read is about: ``portfolio`` if the caller may read
    it, else the default book (operator) or the user's oldest open one."""
    from stonks.accounts import DEFAULT_PORTFOLIO_ID, NotFound, owned_portfolio

    with context.state() as state:
        if portfolio is not None:
            try:
                return owned_portfolio(state, scope, portfolio).id
            except NotFound:
                raise typer.BadParameter(
                    f"no portfolio {portfolio!r}", param_hint="--portfolio"
                ) from None
        if scope.is_service:
            return DEFAULT_PORTFOLIO_ID
        rows = state.sql(
            "SELECT id FROM portfolios WHERE owner_id = ? AND status != 'archived'"
            " ORDER BY id != ?, created_at, id LIMIT 1",
            [scope.user_id, DEFAULT_PORTFOLIO_ID],
        )
    if not rows:
        raise typer.BadParameter("this user has no portfolio yet", param_hint="--user")
    return rows[0]["id"]


def _bps(value: float | None) -> str:
    return "-" if value is None else f"{value:.1f}"


@app.command("summary")
def summary(
    by: str = typer.Option(
        "all", "--by", help="all | strategy | ticker | portfolio | day | week | month"
    ),
    since: str | None = _SINCE,
    until: str | None = typer.Option(None, "--until", help="orders decided on or before this day"),
    strategy: str | None = typer.Option(None, "--strategy", help="one strategy id"),
    ticker: str | None = typer.Option(None, "--ticker", help="one ticker"),
    portfolio: str | None = _PORTFOLIO,
    user: str | None = _USER,
) -> None:
    """Implementation shortfall in bps: delay, impact, fees and opportunity
    cost, next to the cost model's estimate."""
    if by not in GROUP_BYS:
        raise typer.BadParameter(f"one of {', '.join(GROUP_BYS)}", param_hint="--by")
    context, service = _service()
    scope = _who(context, user)
    pf = _portfolio(context, scope, portfolio)
    view = _call(
        lambda: service.summary(
            pf,
            by=by,  # type: ignore[arg-type]
            since=_day(since, "--since"),
            until=_day(until, "--until"),
            strategy_id=strategy,
            ticker=ticker,
        )
    )
    if not view.groups:
        console.print("no orders with a recorded decision")
        return
    table = Table(title=f"implementation shortfall, bps ({pf}, by {by})")
    for col in (
        by,
        "orders",
        "filled",
        "notional",
        "delay",
        "impact",
        "fees",
        "IS",
        "opportunity",
        "convention",
        "model",
        "gap",
    ):
        table.add_column(col)
    for g in view.groups:
        table.add_row(
            g.key,
            str(g.orders),
            str(g.filled_orders),
            f"{g.filled_notional:,.0f}",
            _bps(g.delay_bps),
            _bps(g.impact_bps),
            _bps(g.fee_bps),
            _bps(g.is_bps),
            _bps(g.opportunity_bps),
            _bps(g.convention_bps),
            _bps(g.expected_bps),
            _bps(g.model_gap_bps),
        )
    console.print(table)


@app.command("journal")
def journal(
    since: str | None = _SINCE,
    strategy: str | None = typer.Option(None, "--strategy", help="one strategy id"),
    ticker: str | None = typer.Option(None, "--ticker", help="one ticker"),
    limit: int = typer.Option(50, "--limit", min=1, max=500, help="orders to show"),
    portfolio: str | None = _PORTFOLIO,
    user: str | None = _USER,
) -> None:
    """The trade journal, newest first: reason, signal, outcome and notes."""
    context, service = _service()
    scope = _who(context, user)
    pf = _portfolio(context, scope, portfolio)
    page = _call(
        lambda: service.journal(
            pf, since=_day(since, "--since"), strategy_id=strategy, ticker=ticker, limit=limit
        )
    )
    if not page.items:
        console.print("no orders")
        return
    table = Table(title=f"trade journal ({pf}, {len(page.items)} of {page.total})")
    for col in ("order", "strategy", "side", "status", "trigger", "score", "IS bps", "notes"):
        table.add_column(col)
    for e in page.items:
        score = (e.context or {}).get("score")
        table.add_row(
            e.client_id,
            e.strategy_id or "-",
            e.side,
            e.status,
            e.trigger or "-",
            "-" if score is None else f"{score:.4g}",
            _bps(e.shortfall.is_bps if e.shortfall else None),
            " | ".join(n.note for n in e.notes) or "-",
        )
    console.print(table)


@app.command("order")
def order(
    client_id: str = typer.Argument(..., help="the order's client id"),
    user: str | None = _USER,
) -> None:
    """One order in full: decision, context, shortfall and notes."""
    context, service = _service()
    entry = _call(lambda: service.order(_who(context, user), client_id))
    console.print(f"[bold]{entry.client_id}[/bold] {entry.side} {entry.quantity:g} {entry.ticker}")
    console.print(
        f"  status: {entry.status}" + (f" ({entry.status_reason})" if entry.status_reason else "")
    )
    console.print(f"  strategy: {entry.strategy_id or '-'}, trigger: {entry.trigger or '-'}")
    console.print(f"  decided: {entry.decided_at or '-'} at {entry.decision_price or '-'}")
    for key, value in (entry.context or {}).items():
        console.print(f"  context.{key}: {value}", markup=False)
    if entry.shortfall is not None:
        for key, value in entry.shortfall.model_dump().items():
            shown = "-" if value is None else f"{value:.4g}" if isinstance(value, float) else value
            console.print(f"  {key}: {shown}", markup=False)
    for n in entry.notes:
        console.print(f"  note #{n.id} by {n.author} ({n.updated_at}): {n.note}", markup=False)


@app.command("note")
def note(
    client_id: str = typer.Argument(..., help="the order's client id"),
    text: str = typer.Argument(..., help="the note"),
    user: str | None = _USER,
) -> None:
    """Add a note to an order."""
    from stonks.app.tca import NoteRequest

    context, service = _service()
    view = _call(lambda: service.add_note(_who(context, user), client_id, NoteRequest(note=text)))
    console.print(f"[green]note #{view.id} added[/green] to {client_id}")


@app.command("edit-note")
def edit_note(
    note_id: int = typer.Argument(..., help="the note id"),
    text: str = typer.Argument(..., help="the new text"),
    user: str | None = _USER,
) -> None:
    """Replace the text of a note you wrote."""
    from stonks.app.tca import NoteRequest

    context, service = _service()
    view = _call(lambda: service.update_note(_who(context, user), note_id, NoteRequest(note=text)))
    console.print(f"[green]note #{view.id} updated[/green]")


@app.command("refresh")
def refresh() -> None:
    """Fill the next session's open and close of past orders from the lake
    (the tick does this after every run)."""
    from stonks.production.tca import refresh_benchmarks

    context, _ = _service()
    with context.lake() as lake, context.state() as state:
        updated = refresh_benchmarks(state, lake)
    console.print(f"benchmark prices filled for {updated} order(s)")

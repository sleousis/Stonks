"""``stonks orders``: place, preview, change, cancel and list manual orders
(roadmap 20.1).

Mounted by :mod:`stonks.cli`. Every command runs as the operator
(``service:cli``, any portfolio, ``--portfolio`` required) unless ``--user``
names a user, who then acts only on their own portfolios. An order on a
book that trades real money asks you to type ``PLACE LIVE ORDER``."""

from __future__ import annotations

from typing import Any

import typer
from rich.console import Console
from rich.table import Table

app = typer.Typer(help="Manual orders: place, change and cancel your own", no_args_is_help=True)


class _LazyConsole:
    def print(self, *args: Any, **kwargs: Any) -> None:
        Console().print(*args, **kwargs)


console = _LazyConsole()

_USER = typer.Option(
    None, "--user", help="act as this user (email or id); default: the operator (service:cli)"
)
_PORTFOLIO = typer.Option(
    None, "--portfolio", help="portfolio id (required for the operator; default: the user's own)"
)
_REASON = typer.Option(..., "--reason", help="why (recorded on the order and audited)")


def _service() -> tuple[Any, Any]:
    from stonks.app.context import AppContext
    from stonks.app.manual_orders import ManualOrdersService
    from stonks.cli import _settings

    context = AppContext(_settings())
    with context.state() as state:
        state.migrate()
    with context.lake() as lake:
        lake.migrate()
    return context, ManualOrdersService(context)


def _who(context: Any, user: str | None) -> Any:
    from stonks.cli import _halt_scope

    return _halt_scope(context, user)


def _call(fn: Any, *, live_retry: Any = None) -> Any:
    """Run a service call; a real-money refusal asks for the typed phrase
    and retries with ``live_retry`` once it is typed."""
    from stonks.app.errors import AppError
    from stonks.app.manual_orders import LIVE_PHRASE
    from stonks.auth.errors import PermissionDenied

    try:
        return fn()
    except PermissionDenied as exc:
        if live_retry is None or LIVE_PHRASE not in str(exc):
            raise typer.BadParameter(str(exc)) from None
        typed = typer.prompt(f"This book trades real money. Type {LIVE_PHRASE} to go on")
        if typed.strip() != LIVE_PHRASE:
            raise typer.BadParameter("not confirmed; nothing was placed") from None
        return _call(live_retry)
    except AppError as exc:
        raise typer.BadParameter(str(exc)) from None
    except ValueError as exc:  # request model validation
        raise typer.BadParameter(str(exc)) from None


def _print_result(out: Any) -> None:
    color = {"filled": "green", "pending": "yellow", "preview": "cyan"}.get(out.status, "red")
    console.print(
        f"[{color}]{out.status}[/{color}] {out.side} {out.quantity:g} {out.ticker}"
        f" ({out.client_id}) at ref {out.reference_price:g}"
        + (f", filled at {out.fill_price:g}" if out.fill_price is not None else "")
    )
    if out.reason:
        console.print(f"  {out.reason}")
    if out.halt:
        console.print(f"  halt in force: {out.halt}")
    for adj in out.adjustments:
        console.print(
            f"  risk {adj.rule}: {adj.original_quantity:g} -> {adj.adjusted_quantity:g} ({adj.reason})"
        )
    if out.duplicate:
        console.print("  this client id was placed before; nothing new was sent")


def _request(
    ticker: str,
    side: str,
    quantity: float,
    limit: float | None,
    reason: str,
    portfolio: str | None,
    client_id: str | None,
    allow_reduce: bool,
) -> Any:
    from stonks.app.manual_orders import ManualOrderRequest

    if side not in ("buy", "sell"):
        raise typer.BadParameter("buy or sell", param_hint="--side")
    try:
        return ManualOrderRequest(
            portfolio_id=portfolio,
            ticker=ticker,
            side=side,  # type: ignore[arg-type]
            quantity=quantity,
            order_type="limit" if limit is not None else "market",
            limit_price=limit,
            reason=reason,
            client_id=client_id,
            allow_reduce=allow_reduce,
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from None


_TICKER = typer.Option(..., "--ticker", help="instrument id, e.g. AAPL.US")
_SIDE = typer.Option(..., "--side", help="buy | sell")
_QTY = typer.Option(..., "--quantity", help="shares or units")
_LIMIT = typer.Option(None, "--limit", help="limit price (a limit order); default: market")
_KEY = typer.Option(None, "--client-id", help="your idempotency key")
_REDUCE = typer.Option(False, "--allow-reduce", help="accept a smaller order from the risk rules")


@app.command("place")
def place(
    ticker: str = _TICKER,
    side: str = _SIDE,
    quantity: float = _QTY,
    limit: float | None = _LIMIT,
    reason: str = _REASON,
    portfolio: str | None = _PORTFOLIO,
    client_id: str | None = _KEY,
    allow_reduce: bool = _REDUCE,
    user: str | None = _USER,
) -> None:
    """Place an order through the kill switch, every halt and every risk rule."""
    context, service = _service()
    who = _who(context, user)
    body = _request(ticker, side, quantity, limit, reason, portfolio, client_id, allow_reduce)
    out = _call(
        lambda: service.place(who, body),
        live_retry=lambda: service.place(who, body, confirm_live=True),
    )
    _print_result(out)


@app.command("preview")
def preview(
    ticker: str = _TICKER,
    side: str = _SIDE,
    quantity: float = _QTY,
    limit: float | None = _LIMIT,
    reason: str = typer.Option("preview", "--reason", help="why (not recorded for a preview)"),
    portfolio: str | None = _PORTFOLIO,
    allow_reduce: bool = _REDUCE,
    user: str | None = _USER,
) -> None:
    """Run every check of an order and show what would be placed."""
    context, service = _service()
    who = _who(context, user)
    body = _request(ticker, side, quantity, limit, reason, portfolio, None, allow_reduce)
    _print_result(_call(lambda: service.preview(who, body)))


@app.command("change")
def change(
    client_id: str = typer.Argument(..., help="the working manual order's client id"),
    quantity: float | None = typer.Option(None, "--quantity", help="new quantity"),
    limit: float | None = typer.Option(None, "--limit", help="new limit price"),
    reason: str = _REASON,
    portfolio: str | None = _PORTFOLIO,
    allow_reduce: bool = _REDUCE,
    user: str | None = _USER,
) -> None:
    """Cancel a working manual order and place its replacement."""
    from stonks.app.manual_orders import ManualOrderChange

    context, service = _service()
    who = _who(context, user)
    try:
        body = ManualOrderChange(
            portfolio_id=portfolio,
            quantity=quantity,
            limit_price=limit,
            reason=reason,
            allow_reduce=allow_reduce,
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from None
    out = _call(
        lambda: service.change(who, client_id, body),
        live_retry=lambda: service.change(who, client_id, body, confirm_live=True),
    )
    _print_result(out)


@app.command("cancel")
def cancel(
    client_id: str = typer.Argument(..., help="the working order's client id"),
    reason: str = _REASON,
    portfolio: str | None = _PORTFOLIO,
    user: str | None = _USER,
) -> None:
    """Cancel one working order of a portfolio."""
    from stonks.app.manual_orders import OrderCancelRequest

    context, service = _service()
    who = _who(context, user)
    try:
        body = OrderCancelRequest(portfolio_id=portfolio, reason=reason)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from None
    out = _call(lambda: service.cancel(who, client_id, body))
    word = "[green]cancelled[/green]" if out.cancelled else "[yellow]not cancelled[/yellow]"
    console.print(f"{word}: {out.client_id} is {out.status}")


@app.command("list")
def list_orders(
    portfolio: str | None = _PORTFOLIO,
    manual: bool = typer.Option(False, "--manual", help="only manual orders"),
    limit: int = typer.Option(50, "--limit", min=1, max=500),
    user: str | None = _USER,
) -> None:
    """A portfolio's orders, newest first."""
    from stonks.app.orders import OrdersService
    from stonks.cli_tca import _portfolio

    context, _ = _service()
    scope = _who(context, user)
    pf = _portfolio(context, scope, portfolio)
    page = OrdersService(context).orders(
        origin="manual" if manual else None, limit=limit, offset=0, portfolio_id=pf
    )
    if not page.items:
        console.print("no orders")
        return
    table = Table(title=f"orders ({pf})")
    for col in (
        "client id",
        "origin",
        "side",
        "qty",
        "ticker",
        "type",
        "limit",
        "status",
        "reason",
    ):
        table.add_column(col)
    for o in page.items:
        table.add_row(
            o.client_id,
            o.origin,
            o.side,
            f"{o.quantity:g}",
            o.ticker,
            o.order_type,
            "-" if o.limit_price is None else f"{o.limit_price:g}",
            o.status,
            o.manual_reason or o.status_reason or "",
        )
    console.print(table)

"""``stonks live stage show|report|promote|demote`` and ``stonks live
preview``: the live stages, their gates and the dry-run preview (roadmap
19.9).

Mounted by :mod:`stonks.cli`. The shell runs as the operator
(``service:cli``, any portfolio) and logs changes under ``cli:<os user>``.
A promotion needs a passing gate report computed now and asks you to type
the target stage. A demotion needs a reason only. A preview never sends an
order.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

app = typer.Typer(help="Live stages, gates and the dry-run preview", no_args_is_help=True)
stage_app = typer.Typer(help="A portfolio's live stage and its gate", no_args_is_help=True)
app.add_typer(stage_app, name="stage")


class _LazyConsole:
    def print(self, *args: Any, **kwargs: Any) -> None:
        Console().print(*args, **kwargs)


console = _LazyConsole()

_REASON = typer.Option(..., "--reason", help="why (logged with the change)")
_TO = typer.Option(..., "--to", help="sim_paper, broker_paper, live_small or live_scale")


def _service() -> Any:
    from stonks.app.context import AppContext
    from stonks.app.live import LiveService
    from stonks.cli import _settings

    context = AppContext(_settings())
    with context.state() as state:
        state.migrate()
    return LiveService(context)


def _operator() -> Any:
    from stonks.auth import Principal

    return Principal.service("cli")


def _actor() -> str:
    from stonks.cli import _cli_actor

    return _cli_actor()


def _call[T](fn: Callable[[], T]) -> T:
    """Service errors become a red line and exit code 1."""
    from stonks.app.errors import AppError

    try:
        return fn()
    except AppError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{value:+.2%}"


def _num(value: float | None, fmt: str = ".1f") -> str:
    return "-" if value is None else format(value, fmt)


def _print_stage(view: Any) -> None:
    money = "real money" if view.real_money else "no real money"
    nxt = view.next_stage or "none (top stage)"
    console.print(f"[bold]{view.portfolio_id}[/bold]: {view.stage} ({money}), next: {nxt}")
    if view.days:
        table = Table(title="gate days")
        for col in ("session", "stage", "sent", "filled", "rejected", "refused", "stuck",
                    "no fee", "TCA gap bps", "book", "model", "drift", "clean"):  # fmt: skip
            table.add_column(col)
        for d in view.days:
            table.add_row(
                d.session_date.isoformat(),
                d.stage,
                str(d.orders_sent),
                str(d.orders_filled),
                str(d.orders_rejected),
                str(d.orders_refused),
                str(d.stuck_orders),
                str(d.fills_missing_commission),
                _num(d.tca_gap_bps),
                _pct(d.live_return),
                _pct(d.model_return),
                "-" if d.drift_items is None else str(d.drift_items),
                "[green]yes[/green]" if d.clean else "[red]no[/red]",
            )
        console.print(table)
    for c in view.history[:10]:
        console.print(
            f"  {c.created_at:%Y-%m-%d %H:%M} {c.direction} {c.from_stage} -> {c.to_stage}"
            f" by {c.actor}: {c.reason}"
        )


def _print_report(report: Any) -> None:
    verdict = "[green]passes[/green]" if report.passed else "[red]does not pass[/red]"
    target = report.target or "none"
    console.print(f"gate {report.from_stage} -> {target}: {verdict}")
    for c in report.checks:
        mark = {True: "[green]pass[/green]", False: "[red]fail[/red]", None: "[yellow]n/a[/yellow]"}
        console.print(f"  {mark[c.passed]} {c.name}: {c.detail}")
    m = report.metrics
    console.print(
        f"  sessions {m['sessions']}, clean streak {m['clean_streak']}, reject rate"
        f" {m['reject_rate']:.1%}, TCA gap {_num(m['tca_gap_bps'])} bps, tracking error"
        f" {'-' if m['tracking_error'] is None else format(m['tracking_error'], '.2%')}"
    )


@stage_app.command("show")
def stage_show(
    portfolio: str = typer.Argument(..., help="portfolio id"),
    days: int = typer.Option(20, "--days", min=1, max=260, help="sessions of gate metrics"),
) -> None:
    """The stage, the last sessions' gate metrics and the stage changes."""
    _print_stage(_call(lambda: _service().stage(_operator(), portfolio, days)))


@stage_app.command("report")
def stage_report(portfolio: str = typer.Argument(..., help="portfolio id")) -> None:
    """The gate report for the next stage, computed now."""
    report = _call(lambda: _service().gate_report(_operator(), portfolio))
    _print_report(report)
    if not report.passed:
        raise typer.Exit(code=1)


@stage_app.command("promote")
def stage_promote(
    portfolio: str = typer.Argument(..., help="portfolio id"),
    to: str = _TO,
    reason: str = _REASON,
) -> None:
    """One stage up. Needs a passing gate report and asks you to type the stage."""
    from stonks.app.live import StagePromoteBody

    service = _service()
    report = _call(lambda: service.gate_report(_operator(), portfolio))
    _print_report(report)
    if not report.passed:
        raise typer.Exit(code=1)
    typed = typer.prompt(f"Type {to} to promote {portfolio}")
    body = _call(lambda: _body(StagePromoteBody, to_stage=to, reason=reason, confirm=typed))
    view = _call(lambda: service.promote_from_shell(_operator(), portfolio, body, actor=_actor()))
    console.print(f"[green]promoted[/green]: {portfolio} is now in {view.stage}")


@stage_app.command("demote")
def stage_demote(
    portfolio: str = typer.Argument(..., help="portfolio id"),
    to: str = _TO,
    reason: str = _REASON,
) -> None:
    """Down to a lower stage, with a reason."""
    from stonks.app.live import StageDemoteBody

    body = _call(lambda: _body(StageDemoteBody, to_stage=to, reason=reason))
    view = _call(lambda: _service().demote(_operator(), portfolio, body, actor=_actor()))
    console.print(f"[yellow]demoted[/yellow]: {portfolio} is now in {view.stage}")


@app.command("preview")
def preview(portfolio: str = typer.Argument(..., help="portfolio id")) -> None:
    """The orders the live book would send now, through every rule and the
    broker's what-if. It never sends an order."""
    view = _call(lambda: _service().preview(_operator(), portfolio))
    console.print(
        f"preview of {view.portfolio_id} on {view.as_of} ({view.stage}): {view.status}"
        + (f", {view.reason}" if view.reason else "")
    )
    if view.account is not None:
        a = view.account
        console.print(
            f"  account: equity {a.equity:,.2f} {a.currency}, cash {a.cash:,.2f},"
            f" settled {a.settled_cash:,.2f}, buying power {a.buying_power:,.2f}"
        )
    if view.orders:
        table = Table(title="orders it would send (not sent)")
        for col in ("side", "ticker", "qty", "type", "limit", "notional", "commission",
                    "margin change", "rules"):  # fmt: skip
            table.add_column(col)
        for o in view.orders:
            w = o.what_if
            table.add_row(
                o.side,
                o.ticker,
                f"{o.quantity:g}",
                o.order_type + (f" {o.time_in_force}" if o.time_in_force else ""),
                _num(o.limit_price, ".2f"),
                _num(o.notional, ",.2f"),
                o.what_if_error or ("-" if w is None else _num(w.commission, ".2f")),
                "-" if w is None else f"{w.initial_margin_change:,.2f}",
                ", ".join(sorted({a.rule for a in o.adjustments})) or "-",
            )
        console.print(table)
    else:
        console.print("  no orders")
    dropped = [a for a in view.adjustments if a.adjusted_quantity <= 0]
    for a in dropped:
        console.print(f"  dropped {a.side} {a.ticker} by {a.rule}: {a.reason}")
    for note in view.notes:
        console.print(f"  [yellow]{note}[/yellow]")
    console.print("[dim]nothing was sent to the broker[/dim]")


def _body(model: Any, **values: Any) -> Any:
    from pydantic import ValidationError as PydanticError

    from stonks.app.errors import ValidationError

    try:
        return model(**values)
    except PydanticError as exc:
        raise ValidationError(str(exc.errors()[0]["msg"])) from None

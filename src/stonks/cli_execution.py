"""``stonks algos`` and ``stonks plan``: execution algos per portfolio and
the rebalancing planner (roadmap 23.16).

Mounted by :mod:`stonks.cli`. Every command runs as the operator
(``service:cli``, every portfolio) unless ``--user`` names a user, who then
acts on their own portfolios only. ``plan preview`` sends nothing.
``plan confirm`` writes order tickets that wait for approval
(``stonks tickets approve``); it asks you to confirm unless ``--yes``."""

from __future__ import annotations

import json
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

algos_app = typer.Typer(
    help="Execution algos: how a portfolio's orders are worked", no_args_is_help=True
)
plan_app = typer.Typer(
    help="Rebalancing planner: the trades to reach target weights", no_args_is_help=True
)


class _LazyConsole:
    def print(self, *args: Any, **kwargs: Any) -> None:
        Console().print(*args, **kwargs)


console = _LazyConsole()

_USER = typer.Option(
    None, "--user", help="act as this user (email or id); default: the operator (service:cli)"
)
_PORTFOLIO = typer.Option(..., "--portfolio", help="the portfolio id")


def _service() -> tuple[Any, Any]:
    from stonks.app.context import AppContext
    from stonks.app.planner import ExecutionService
    from stonks.cli import _settings

    context = AppContext(_settings())
    with context.state() as state:
        state.migrate()
    return context, ExecutionService(context)


def _who(context: Any, user: str | None) -> Any:
    from stonks.cli import _halt_scope

    return _halt_scope(context, user)


def _call(fn: Any) -> Any:
    from stonks.app.errors import AppError
    from stonks.auth.errors import PermissionDenied

    try:
        return fn()
    except (AppError, PermissionDenied) as exc:
        raise typer.BadParameter(str(exc)) from None
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from None


# ---- stonks algos ---------------------------------------------------------------------


@algos_app.command("list")
def algos_list(user: str | None = _USER) -> None:
    """Every execution algo, its defaults and the costs the backtest assumes."""
    context, service = _service()
    items = _call(lambda: service.algos(_who(context, user))).items
    table = Table(title="execution algos")
    for col in ("name", "slices at other brokers", "defaults", "cost assumption"):
        table.add_column(col)
    for a in items:
        table.add_row(
            a.name,
            "yes" if a.sliceable else "no (plain order)",
            json.dumps(a.defaults),
            ", ".join(f"{k} {v:g}" for k, v in a.cost_assumption.items()),
        )
    console.print(table)


@algos_app.command("show")
def algos_show(portfolio: str = _PORTFOLIO, user: str | None = _USER) -> None:
    """The portfolio's algo setting and each strategy's override."""
    context, service = _service()
    items = _call(lambda: service.settings(_who(context, user), portfolio)).items
    if not items:
        console.print("plain orders (no algo set)")
        return
    for s in items:
        who = s.strategy_id or "portfolio"
        console.print(f"{who}: {s.algo} {json.dumps(s.params)} (by {s.updated_by or '-'})")


@algos_app.command("set")
def algos_set(
    algo: str = typer.Argument(..., help="adaptive | twap | vwap"),
    portfolio: str = _PORTFOLIO,
    params: str = typer.Option("{}", "--params", help="JSON, e.g. '{\"end_minutes\": 120}'"),
    strategy: str | None = typer.Option(None, "--strategy", help="only this strategy's orders"),
    user: str | None = _USER,
) -> None:
    """Work the portfolio's orders (or one strategy's) with an algo."""
    from stonks.app.planner import AlgoSettingUpdate

    try:
        parsed = json.loads(params)
    except json.JSONDecodeError as exc:
        raise typer.BadParameter(str(exc), param_hint="--params") from None
    context, service = _service()
    body = _call(lambda: AlgoSettingUpdate(algo=algo, params=parsed, strategy_id=strategy))
    s = _call(lambda: service.set_algo(_who(context, user), portfolio, body))
    console.print(
        f"[green]set[/green] {s.strategy_id or 'portfolio'}: {s.algo} {json.dumps(s.params)}"
    )


@algos_app.command("clear")
def algos_clear(
    portfolio: str = _PORTFOLIO,
    strategy: str | None = typer.Option(None, "--strategy", help="only this strategy's override"),
    user: str | None = _USER,
) -> None:
    """Back to plain orders (or a strategy back to the portfolio's setting)."""
    context, service = _service()
    removed = _call(lambda: service.clear_algo(_who(context, user), portfolio, strategy))
    console.print("cleared" if removed else "nothing to clear")


@algos_app.command("parents")
def algos_parents(portfolio: str = _PORTFOLIO, user: str | None = _USER) -> None:
    """Parent orders Stonks works as child slices, newest first."""
    context, service = _service()
    items = _call(lambda: service.parents(_who(context, user), portfolio)).items
    if not items:
        console.print("no parent orders")
        return
    table = Table(title="parent orders")
    for col in ("order", "algo", "state", "filled", "window", "slices"):
        table.add_column(col)
    for p in items:
        sent = sum(1 for s in p.slices if s.status == "sent")
        table.add_row(
            f"{p.side} {p.quantity:g} {p.ticker}",
            p.algo,
            p.state,
            f"{p.filled:g}",
            f"{p.window_start:%H:%M}-{p.window_end:%H:%M} UTC",
            f"{sent}/{len(p.slices)} sent",
        )
    console.print(table)


# ---- stonks plan ----------------------------------------------------------------------


def _request(
    portfolio: str,
    strategy: str | None,
    targets: str | None,
    min_trade: float,
    short_rate: float | None,
    long_rate: float | None,
) -> dict[str, Any]:
    if (strategy is None) == (targets is None):
        raise typer.BadParameter("give --strategy or --targets (TICKER=WEIGHT,...)")
    body: dict[str, Any] = {
        "portfolio_id": portfolio,
        "min_trade_value": min_trade,
        "short_term_rate": short_rate,
        "long_term_rate": long_rate,
    }
    if strategy is not None:
        return {**body, "source": "strategy", "strategy_id": strategy}
    pairs = []
    for part in (targets or "").split(","):
        ticker, _, weight = part.strip().partition("=")
        try:
            pairs.append({"ticker": ticker.strip(), "weight": float(weight)})
        except ValueError:
            raise typer.BadParameter(
                f"{part!r} is not TICKER=WEIGHT", param_hint="--targets"
            ) from None
    return {**body, "source": "targets", "targets": pairs}


_STRATEGY = typer.Option(None, "--strategy", help="plan to this strategy's model weights")
_TARGETS = typer.Option(None, "--targets", help="or your own list: AAPL.US=0.3,MSFT.US=0.2")
_MIN_TRADE = typer.Option(0.0, "--min-trade", help="skip trades below this value")
_SHORT = typer.Option(None, "--short-term-rate", help="tax rate on short-term gains (0-1)")
_LONG = typer.Option(None, "--long-term-rate", help="tax rate on long-term gains (0-1)")


def _print_plan(p: Any) -> None:
    table = Table(title=f"plan for {p.portfolio_id} on {p.as_of.isoformat()}")
    for col in ("ticker", "now", "target", "trade", "value", "cost", "after", "tax", "note"):
        table.add_column(col)
    for line in p.lines:
        trade = f"{line.side} {line.quantity:g}" if line.side else "-"
        tax = "-"
        if line.tax is not None:
            tax = f"gain {line.tax.gain:,.2f}"
            if line.tax.estimated_tax is not None:
                tax += f", tax {line.tax.estimated_tax:,.2f}"
        cost = f"{line.cost:,.2f}" + (f" ({line.cost_bps:.1f} bps)" if line.cost_bps else "")
        table.add_row(
            line.ticker,
            f"{line.current_weight:.1%}",
            f"{line.target_weight:.1%}",
            trade,
            f"{line.value:,.2f}",
            cost,
            f"{line.weight_after:.1%}",
            tax,
            line.skipped or "",
        )
    console.print(table)
    console.print(
        f"value {p.equity:,.2f}  turnover {p.turnover:.1%}  cost {p.total_cost:,.2f}"
        f"  cash after {p.cash_after:,.2f}  largest drift after {p.max_drift_after:.2%}"
    )
    if p.tax_total is not None:
        console.print(f"estimated tax {p.tax_total:,.2f} (a preview, not a tax report)")
    if p.algo:
        console.print(f"worked with {p.algo['name']} {json.dumps(p.algo.get('params', {}))}")
    for note in p.notes:
        console.print(f"[yellow]note[/yellow] {note}")


@plan_app.command("preview")
def plan_preview(
    portfolio: str = _PORTFOLIO,
    strategy: str | None = _STRATEGY,
    targets: str | None = _TARGETS,
    min_trade: float = _MIN_TRADE,
    short_rate: float | None = _SHORT,
    long_rate: float | None = _LONG,
    user: str | None = _USER,
) -> None:
    """The trades that reach the targets, with costs, turnover and a tax
    preview. Nothing is written or sent."""
    from stonks.app.planner import PlanRequest

    context, service = _service()
    body = _call(lambda: PlanRequest(**_request(portfolio, strategy, targets, min_trade,
                                                short_rate, long_rate)))  # fmt: skip
    _print_plan(_call(lambda: service.plan(_who(context, user), body)))


@plan_app.command("confirm")
def plan_confirm(
    portfolio: str = _PORTFOLIO,
    reason: str = typer.Option(..., "--reason", help="why you rebalance (kept and audited)"),
    strategy: str | None = _STRATEGY,
    targets: str | None = _TARGETS,
    min_trade: float = _MIN_TRADE,
    short_rate: float | None = _SHORT,
    long_rate: float | None = _LONG,
    yes: bool = typer.Option(False, "--yes", help="do not ask before writing the tickets"),
    user: str | None = _USER,
) -> None:
    """Write one order ticket per trade. Each waits for approval
    (``stonks tickets approve``) and goes out in the submit window."""
    from stonks.app.planner import PlanConfirm, PlanRequest

    context, service = _service()
    who = _who(context, user)
    fields = _request(portfolio, strategy, targets, min_trade, short_rate, long_rate)
    preview = _call(lambda: service.plan(who, _call(lambda: PlanRequest(**fields))))
    _print_plan(preview)
    trades = [line for line in preview.lines if line.side]
    if not trades:
        console.print("nothing to trade")
        return
    if not yes and not typer.confirm(f"Write {len(trades)} ticket(s) for approval?"):
        raise typer.Abort()
    body = _call(lambda: PlanConfirm(**fields, reason=reason))
    done = _call(lambda: service.confirm(who, body))
    console.print(
        f"[green]{done.written} ticket(s) written[/green] (key {done.key}):"
        " approve them with stonks tickets approve"
    )
    for tid in done.ticket_ids:
        console.print(f"  {tid}")

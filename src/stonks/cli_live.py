"""``stonks live``: going live from the shell.

- ``live stage show|report|promote|demote`` and ``live preview``: the live
  stages, their gates and the dry-run preview (roadmap 19.9). A promotion
  needs a passing gate report computed now and asks you to type the target
  stage. A demotion needs a reason only. A preview never sends an order.
- ``live soak-report`` sums up N trading days of a broker portfolio's paper
  trading (``production.soak``). It only reads the state DB.
- ``live reconcile`` syncs a portfolio's open orders with its broker now.
- ``halts drill`` runs the kill switch drill (``production.drills``) on a
  scratch state DB with the simulated working-order broker. It never
  touches production data and never sends a real order (roadmap 19.11).

Mounted by :mod:`stonks.cli`. The shell runs as the operator
(``service:cli``, any portfolio) and logs changes under ``cli:<os user>``.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

app = typer.Typer(
    help="Going live: stages, gates, the dry-run preview, the paper soak report and reconcile",
    no_args_is_help=True,
)
stage_app = typer.Typer(help="A portfolio's live stage and its gate", no_args_is_help=True)
app.add_typer(stage_app, name="stage")


class _LazyConsole:
    def print(self, *args: Any, **kwargs: Any) -> None:
        Console().print(*args, **kwargs)


console = _LazyConsole()

#: Brokers the drill may use. Only the simulated one: the drill never sends
#: a real order (the IBKR paper round trip is the live contract tests' job).
DRILL_BROKERS = ("simulated",)


def _settings() -> Any:
    from stonks.cli import _settings as load

    return load()


def _parse_day(value: str | None) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise typer.BadParameter(f"not a date: {value!r} (use YYYY-MM-DD)") from None


@app.command("soak-report")
def soak_report_cmd(
    portfolio: str = typer.Option(..., "--portfolio", help="the broker portfolio id"),
    days: int = typer.Option(20, "--days", min=1, help="trading days to look back"),
    end: str | None = typer.Option(None, "--end", help="last day (YYYY-MM-DD), default today"),
    model: str | None = typer.Option(
        None, "--model", help="model book portfolio id (default: the paper twin)"
    ),
    as_json: bool = typer.Option(False, "--json", help="print JSON"),
    strict: bool = typer.Option(False, "--strict", help="exit 1 when the soak is not clean"),
) -> None:
    """Sum up N trading days of paper trading at the broker: outcomes,
    slippage, fills against the model book, gateway outages and drift."""
    from stonks.app.drills import live_soak_report

    report = live_soak_report(
        _settings(),
        portfolio_id=portfolio,
        days=days,
        end=_parse_day(end),
        model_portfolio_id=model,
    )
    if as_json:
        typer.echo(json.dumps(report.to_dict(), indent=2, default=str))
    else:
        _print_soak(report)
    if strict and not report.clean:
        raise typer.Exit(code=1)


@app.command("reconcile")
def reconcile_cmd(
    portfolio: str = typer.Option("pf_default", "--portfolio", help="the portfolio id"),
) -> None:
    """Sync a portfolio's open orders and fills with its broker now, as the
    submit job does before it sends. Reads the broker, never sends."""
    from stonks.app.drills import reconcile_portfolio

    out = reconcile_portfolio(_settings(), portfolio_id=portfolio)
    if out is None:
        console.print(f"{portfolio} trades at no external broker: nothing to reconcile")
        return
    s = out.summary
    console.print(
        f"checked {s.orders_checked} orders, updated {s.orders_updated}, "
        f"booked {s.fills_inserted} fills"
    )
    for label, ids in (
        ("still unknown", out.unresolved),
        ("failed", s.failed_orders),
        ("fills with no order", s.orphan_fills),
    ):
        if ids:
            console.print(f"[yellow]{label}[/yellow]: {', '.join(ids)}")
    if not out.ok:
        raise typer.Exit(code=1)


@app.command("journal")
def journal_cmd(
    paths: list[Path] = typer.Argument(..., help="journal files (.jsonl) or folders"),
    kind: list[str] = typer.Option(
        [], "--kind", help="only these kinds: call, order_status, execution, error"
    ),
    as_json: bool = typer.Option(False, "--json", help="print the events as JSON lines"),
) -> None:
    """Read the broker event journal ([brokers.ibkr.journal], roadmap 23.15):
    each gateway call in order, with order status, executions and errors.
    Replay a journal in a test with ``ReplayIbClient.from_files``."""
    from stonks.execution.brokers.ibkr.journal import journal_summary, read_journal

    files: list[Path] = []
    for path in paths:
        files.extend(sorted(path.rglob("*.jsonl")) if path.is_dir() else [path])
    missing = [str(f) for f in files if not f.is_file()]
    if missing or not files:
        raise typer.BadParameter(f"no journal file at {', '.join(missing) or 'those paths'}")
    events = read_journal(files)
    if kind:
        events = [e for e in events if e.kind in set(kind)]
    if as_json:
        for e in events:
            typer.echo(json.dumps(e.as_dict(), default=str))
        return
    table = Table("seq", "at", "kind", "call", "detail")
    for row in journal_summary(events):
        table.add_row(str(row["seq"]), row["at"], row["kind"], row["method"], row["detail"])
    console.print(table)
    errors = sum(1 for e in events if e.kind == "error")
    console.print(f"{len(events)} events, {errors} errors")


def _bps(value: float | None) -> str:
    return "-" if value is None else f"{value:.1f} bps"


def _print_soak(report: Any) -> None:
    table = Table(title=f"paper soak {report.portfolio_id} ({report.start} to {report.end})")
    table.add_column("item")
    table.add_column("value")
    rows: list[tuple[str, str]] = [
        ("trading days", f"{report.days_observed} of {report.days_requested}"),
        ("orders", str(report.orders)),
        ("filled / partial", f"{report.filled} / {report.partially_filled}"),
        ("rejected", f"{report.rejected} ({report.reject_rate:.1%})"),
        ("cancelled", str(report.cancelled)),
        ("unknown outcome", str(report.unknown)),
        ("slippage mean / worst", f"{_bps(report.slippage.mean_bps)} / "
                                  f"{_bps(report.slippage.worst_bps)}"),
        ("gateway outages / recovered", f"{report.outages} / {report.reconnects}"),
        ("broker drift halts", str(report.drift_halts)),
    ]  # fmt: skip
    vs = report.vs_model
    if vs is None:
        rows.append(("model book", "none"))
    else:
        rows.append(
            (
                f"vs model {vs.model_portfolio_id}",
                f"{vs.matched} matched, {vs.model_only} missing, {vs.broker_only} extra, "
                f"price gap {_bps(vs.price_gap_bps)}",
            )
        )
    drift = report.drift
    rows.append(
        (
            "reconcile reports",
            "no table" if drift is None else f"{drift.reports}, {drift.with_drift} with drift",
        )
    )
    for reason, count in report.reject_reasons.items():
        rows.append((f"  rejected: {reason}", str(count)))
    for item, value in rows:
        table.add_row(item, value)
    console.print(table)
    if report.clean:
        console.print("[green]clean[/green]")
        return
    console.print("[yellow]not clean[/yellow]")
    for finding in report.findings:
        console.print(f"  - {finding}")


def register(halts_app: typer.Typer) -> None:
    """Add ``drill`` to the ``halts`` group."""

    @halts_app.command("drill")
    def halts_drill(
        broker: str = typer.Option(
            "simulated", "--broker", help=f"one of {list(DRILL_BROKERS)} (never a real broker)"
        ),
        ticker: str = typer.Option("AAPL.US", "--ticker", help="the drill order's ticker"),
        price: float = typer.Option(
            100.0, "--price", min=0.01, help="reference price; the limit sits at half of it"
        ),
        timeout: float = typer.Option(
            10.0, "--timeout", min=0.1, help="seconds the cancel may take"
        ),
        json_out: str | None = typer.Option(
            None, "--json-out", help="also write the report as JSON to this file"
        ),
    ) -> None:
        """Kill switch drill, dry run: on a scratch state, place a tiny limit
        far from the market, engage the global kill switch, and check that new
        orders stop and the working order is cancelled at the broker."""
        from stonks.app.drills import run_kill_switch_drill_scratch

        if broker not in DRILL_BROKERS:
            raise typer.BadParameter(
                f"--broker must be one of {list(DRILL_BROKERS)}", param_hint="--broker"
            )
        report = run_kill_switch_drill_scratch(
            _settings(), ticker=ticker, reference_price=price, cancel_timeout=timeout
        )
        if json_out:
            Path(json_out).write_text(json.dumps(report.to_dict(), indent=2, default=str))
        table = Table(title=f"kill switch drill ({report.broker} broker, scratch state)")
        for col in ("step", "result", "detail", "ms"):
            table.add_column(col)
        for s in report.steps:
            mark = "[green]ok[/green]" if s.ok else "[red]failed[/red]"
            table.add_row(s.name, mark, s.detail, f"{s.elapsed_ms:g}")
        console.print(table)
        console.print("[green]drill passed[/green]" if report.passed else "[red]drill failed[/red]")
        if not report.passed:
            raise typer.Exit(code=1)


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

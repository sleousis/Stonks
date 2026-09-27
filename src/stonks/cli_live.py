"""``stonks live soak-report`` and ``stonks halts drill`` (roadmap 19.11).

- ``live soak-report`` sums up N trading days of a broker portfolio's paper
  trading (``production.soak``). It only reads the state DB.
- ``halts drill`` runs the kill switch drill (``production.drills``) on a
  scratch state DB with the simulated working-order broker. It never
  touches production data and never sends a real order.

Both go through ``app.drills``, like every transport.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

app = typer.Typer(help="Going live: the paper soak report", no_args_is_help=True)


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

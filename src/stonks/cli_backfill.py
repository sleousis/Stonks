"""``stonks ingest backfill``: the resumable EODHD backfill (docs/backfill.md).
Mounted under ``stonks ingest`` by :mod:`stonks.cli`.

``run`` writes the lake, so it needs the lake free (stop ``stonks serve``).
``status`` and ``retry`` only touch the state database."""

from __future__ import annotations

import json
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

app = typer.Typer(help="Resumable full backfill from EODHD", no_args_is_help=True)
console = Console()

_PHASES = typer.Option(
    None, "--phases", help="comma-separated phase ids (default: all, see `status`)"
)


def _split(value: str | None) -> list[str]:
    return [t.strip() for t in (value or "").split(",") if t.strip()]


def _settings() -> Any:
    from stonks.cli import _settings as load

    return load()


def _state(settings: Any) -> Any:
    from stonks.ingest.backfill.state import BackfillState
    from stonks.store.state import SqliteState

    sqlite = SqliteState(settings.state.path)
    state = BackfillState(sqlite)
    if not state.ready():
        sqlite.close()
        console.print("[red]the state database has no backfill tables:[/red] run `stonks db init`")
        raise typer.Exit(code=2)
    return sqlite, state


@app.command("run")
def run(
    phases: str | None = _PHASES,
    exchanges: str | None = typer.Option(
        None,
        "--exchanges",
        help="only units of these exchange codes, e.g. US,LSE (market-wide phases still run)",
    ),
    max_calls: int | None = typer.Option(
        None, "--max-calls", min=1, help="stop after this many API calls in this run"
    ),
    budget: int | None = typer.Option(
        None, "--budget", min=1, help="API calls a UTC day may reach (default [ingest.backfill])"
    ),
    until_budget: bool = typer.Option(
        False,
        "--until-budget",
        help="exit when today's budget is spent instead of sleeping until the reset",
    ),
) -> None:
    """Fetch everything the plan offers, phase by phase, resuming where the
    last run stopped. Sleeps through each UTC midnight reset unless
    --until-budget is given (then a scheduler reruns it)."""
    from stonks.ingest.backfill.service import (
        BackfillSetupError,
        build_runner,
        check_plan,
        limit_duckdb,
        make_pipeline_factory,
    )
    from stonks.ingest.sources.registry import SourceConfigError, build_eodhd
    from stonks.store.lake import DuckDBLake

    settings = _settings()
    cfg = settings.ingest.backfill
    try:
        check_plan("eodhd", settings.ensure.plans)
        source = build_eodhd(
            settings.sources, metadata_workers=cfg.network_threads, news_limit=cfg.news_limit
        )
    except (BackfillSetupError, SourceConfigError) as exc:
        console.print(f"[red]cannot start:[/red] {exc}")
        raise typer.Exit(code=2) from exc
    sqlite, state = _state(settings)
    try:
        with DuckDBLake(settings.lake.path) as lake:
            if "instruments" not in lake.tables():
                console.print("[red]the lake is not migrated:[/red] run `stonks db init`")
                raise typer.Exit(code=2)
            limit_duckdb(lake, cfg)
            runner, _budget = build_runner(
                source=source,
                lake=lake,
                state=state,
                settings=cfg,
                make_pipeline=make_pipeline_factory(
                    lake, settings.ingest.quality, settings.ensure.adjustment_tolerance
                ),
                phases=_split(phases) or None,
                exchanges=_split(exchanges) or None,
                max_calls=max_calls,
                until_budget=until_budget,
                daily_budget=budget,
            )
            report = runner.run()
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from exc
    finally:
        sqlite.close()
        close = getattr(source, "close", None)
        if close is not None:
            close()
    console.print(
        f"stopped: [bold]{report.stopped}[/bold] ({report.detail}); "
        f"{report.units_done} done, {report.units_retry} to retry, "
        f"{report.units_failed} failed, {report.calls} API calls"
    )


@app.command("status")
def status(
    phases: str | None = _PHASES,
    as_json: bool = typer.Option(False, "--json", help="print JSON"),
) -> None:
    """Per phase: done, failed, waiting to retry and left, calls spent,
    calls still needed; today's calls and the days left at the budget."""
    from stonks.ingest.backfill.service import backfill_status

    settings = _settings()
    sqlite, state = _state(settings)
    try:
        report = backfill_status(state, settings.ingest.backfill, phases=_split(phases) or None)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from exc
    finally:
        sqlite.close()
    today = report.today
    if as_json:
        payload = {
            "phases": [
                {
                    "phase": p.phase,
                    "title": p.title,
                    "listed": p.listed,
                    "done": p.done,
                    "failed": p.failed,
                    "retry": p.retry,
                    "remaining": p.remaining,
                    "calls": p.calls,
                    "rows": p.rows,
                    "calls_left": p.calls_left,
                }
                for p in report.phases
            ],
            "today": {
                "day": today.day.isoformat(),
                "calls": today.calls,
                "vendor_used": today.vendor_used,
                "vendor_limit": today.vendor_limit,
            },
            "daily_budget": report.daily_budget,
            "calls_left": report.calls_left,
            "eta_days": round(report.eta_days, 1),
        }
        console.print_json(json.dumps(payload))
        return
    table = Table(title="backfill")
    for column in ("phase", "done", "failed", "retry", "left", "calls", "calls left"):
        table.add_column(column, justify="left" if column == "phase" else "right")
    for p in report.phases:
        if not p.listed:
            table.add_row(p.phase, "", "", "", "not listed yet", "", "")
            continue
        table.add_row(
            p.phase,
            f"{p.done:,}",
            f"{p.failed:,}",
            f"{p.retry:,}",
            f"{p.remaining:,}",
            f"{p.calls:,}",
            f"{p.calls_left or 0:,}",
        )
    console.print(table)
    vendor = (
        f", the vendor counted {today.vendor_used:,} of {today.vendor_limit or 0:,}"
        if today.vendor_used is not None
        else ""
    )
    console.print(f"today ({today.day} UTC): {today.calls:,} calls by the backfill{vendor}")
    console.print(
        f"about {report.calls_left:,} calls left for listed phases, "
        f"{report.eta_days:.1f} days at {report.daily_budget:,} a day"
    )


@app.command("retry")
def retry(phases: str | None = _PHASES) -> None:
    """Give failed units a fresh set of attempts on the next run."""
    settings = _settings()
    sqlite, state = _state(settings)
    try:
        count = state.retry_failed(_split(phases) or None)
    finally:
        sqlite.close()
    console.print(f"{count} failed units will be tried again")

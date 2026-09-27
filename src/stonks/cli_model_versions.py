"""``stonks registry versions|version-history|candidates|retrain|swap-check|swap|reject``:
model versions under one strategy id (roadmap 22.6).

Mounted on the ``registry`` group by :mod:`stonks.cli`. Every command goes
through ``app.model_versions.ModelVersionService``, like the API and MCP.
A swap needs a passing swap check, or ``--override`` with a ``--reason`` of
at least 20 characters. Every change is logged under ``cli:<os user>``.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from typing import Any

import typer
from rich.console import Console
from rich.table import Table


class _LazyConsole:
    def print(self, *args: Any, **kwargs: Any) -> None:
        Console().print(*args, **kwargs)


console = _LazyConsole()


def _service() -> Any:
    from stonks.app.context import AppContext
    from stonks.app.model_versions import ModelVersionService
    from stonks.cli import _settings

    context = AppContext(_settings())
    with context.state() as state:
        state.migrate()
    return ModelVersionService(context)


def _actor() -> str:
    from stonks.cli import _cli_actor

    return _cli_actor()


def _run[T](fn: Callable[[], T]) -> T:
    """Service errors become a red line and exit code 1."""
    from stonks.app.errors import AppError
    from stonks.app.model_versions import SwapRefusedError

    try:
        return fn()
    except SwapRefusedError as exc:
        console.print(f"[red]swap refused: {exc}[/red]")
        for line in exc.failures:
            console.print(f"  - {line}")
        console.print(
            "See `stonks registry swap-check <id> <version>`, or pass --override with a "
            "--reason of at least 20 characters."
        )
        raise typer.Exit(code=1) from None
    except AppError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{value:+.2%}"


def register(app: typer.Typer) -> None:
    """Add the version commands to the ``registry`` group."""

    @app.command("versions")
    def versions(strategy_id: str) -> None:
        """A strategy's model versions: live, candidates, archived, rejected, failed."""
        rows = _run(lambda: _service().list(strategy_id))
        table = Table(title=f"model versions: {strategy_id}")
        for col in ("version", "status", "train window", "created", "by", "note"):
            table.add_column(col)
        for v in rows:
            window = f"{v.train_start} .. {v.train_end}" if v.train_end else "registered fit"
            table.add_row(
                str(v.version), v.status, window, v.created_at, v.created_by, v.error or ""
            )
        console.print(table)

    @app.command("version-history")
    def version_history(strategy_id: str) -> None:
        """The strategy's append-only version log, oldest first."""
        rows = _run(lambda: _service().history(strategy_id))
        table = Table(title=f"version history: {strategy_id}")
        for col in ("when", "version", "kind", "change", "actor", "check", "reason"):
            table.add_column(col)
        for e in rows:
            check = "-" if e.check_passed is None else ("pass" if e.check_passed else "fail")
            if e.override:
                check += " (override)"
            table.add_row(
                e.created_at,
                str(e.version),
                e.kind,
                f"{e.from_status or '-'} → {e.to_status}",
                e.actor,
                check,
                e.reason,
            )
        console.print(table)

    @app.command("candidates")
    def candidates() -> None:
        """Every candidate version running as a model book."""
        rows = _run(lambda: _service().candidates())
        if not rows:
            console.print("no candidate versions")
            return
        table = Table(title="candidate versions")
        for col in ("strategy", "version", "book", "train end", "created"):
            table.add_column(col)
        for v in rows:
            table.add_row(v.strategy_id, str(v.version), v.book_id, str(v.train_end), v.created_at)
        console.print(table)

    @app.command("retrain")
    def retrain(
        strategy_ids: list[str] | None = typer.Argument(  # noqa: B008
            None, help="strategies to refit (default: every retrainable one)"
        ),
        as_of: str | None = typer.Option(
            None, "--as-of", help="last day of the training window (YYYY-MM-DD, default today)"
        ),
        force: bool = typer.Option(False, "--force", help="refit even when a recent fit exists"),
        tickers: str | None = typer.Option(
            None, "--tickers", help="comma-separated tickers when the lab universe is unknown"
        ),
    ) -> None:
        """Refit strategies that learn from data into candidate versions.
        Nothing trades until a swap. Opens the lake: stop `stonks serve`
        first, or start the job through the API."""
        from stonks.app.model_versions import RetrainRequest

        request = RetrainRequest(
            strategy_ids=list(strategy_ids) if strategy_ids else None,
            as_of=date.fromisoformat(as_of) if as_of else None,
            force=force,
            tickers=[t.strip() for t in tickers.split(",") if t.strip()] if tickers else None,
        )
        result = _run(lambda: _service().retrain(request, actor=_actor()))
        table = Table(title=f"retrain up to {result.as_of}")
        for col in ("strategy", "status", "version", "detail"):
            table.add_column(col)
        for o in result.outcomes:
            table.add_row(
                o.strategy_id,
                o.status,
                "-" if o.version is None else str(o.version),
                o.detail or "",
            )
        console.print(table)
        console.print(
            f"{result.candidates} candidate(s), {result.failed} failed, {result.skipped} skipped"
        )
        if result.failed:
            raise typer.Exit(code=1)

    @app.command("swap-check")
    def swap_check(strategy_id: str, version: int) -> None:
        """The swap check of a candidate against the live version."""
        report = _run(lambda: _service().check(strategy_id, version))
        verdict = "[green]PASS[/green]" if report.passed else "[red]FAIL[/red]"
        console.print(
            f"{strategy_id} v{version} vs live v{report.live_version}: {verdict}  "
            f"days={report.days} candidate={_pct(report.candidate_return)} "
            f"live={_pct(report.live_return)}"
        )
        for c in report.checks:
            mark = "[green]ok[/green]" if c.passed else "[red]fail[/red]"
            console.print(f"  {mark} {c.name}: {c.detail}")
        if not report.passed:
            raise typer.Exit(code=1)

    @app.command("swap")
    def swap(
        strategy_id: str,
        version: int,
        reason: str | None = typer.Option(None, "--reason", help="why (logged)"),
        override: bool = typer.Option(
            False, "--override", help="swap without a passing check (needs --reason, >= 20 chars)"
        ),
    ) -> None:
        """Make a candidate version live: the next tick trades it. Audited."""
        view = _run(
            lambda: _service().swap(
                strategy_id, version, actor=_actor(), reason=reason, override=override
            )
        )
        console.print(f"[green]{strategy_id} v{view.version} is live[/green]")

    @app.command("reject")
    def reject(
        strategy_id: str,
        version: int,
        reason: str | None = typer.Option(None, "--reason", help="why (logged, required)"),
    ) -> None:
        """Drop a candidate version. Its model book stops."""
        _run(lambda: _service().reject(strategy_id, version, actor=_actor(), reason=reason))
        console.print(f"[yellow]{strategy_id} v{version} rejected[/yellow]")

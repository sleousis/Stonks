"""``stonks lab verify [TARGET ...]``: rerun lab results from their stored
manifests and report whether they moved (roadmap 23.9).

Mounted on the ``lab`` group by :mod:`stonks.cli`. Runs in this process
through ``app.lab_verify.LabVerifyService``, like the API job. Exit code 1
when any result moved.
"""

from __future__ import annotations

import json
from typing import Any

import typer
from rich.console import Console


def _service() -> Any:
    from stonks.app.context import AppContext
    from stonks.app.lab import lab_objectives
    from stonks.app.lab_verify import LabVerifyService
    from stonks.cli import _settings

    context = AppContext(_settings())
    with context.state() as state:
        state.migrate()
    return LabVerifyService(context, objectives=lab_objectives())


def _num(value: float | None) -> str:
    return "-" if value is None else f"{value:.4f}"


def register(app: typer.Typer) -> None:
    """Add ``verify`` to the ``lab`` group."""

    @app.command("verify")
    def verify(
        targets: list[str] = typer.Argument(  # noqa: B008
            None, help="lab run ids or strategy ids (default: every strategy of [lab.verify])"
        ),
        tolerance: float | None = typer.Option(
            None, "--tolerance", min=0.0, help="allowed score drift ([lab.verify] tolerance)"
        ),
        alert: bool = typer.Option(False, "--alert", help="raise an operator alert on a move"),
        as_json: bool = typer.Option(False, "--json", help="print the result as JSON"),
    ) -> None:
        """Rerun lab results from their stored manifests and report drift."""
        from stonks.app.errors import AppError
        from stonks.app.lab_verify import VerifyRequest

        console = Console()
        try:
            result = _service().verify(
                VerifyRequest(targets=list(targets or []), tolerance=tolerance, alert=alert)
            )
        except AppError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from None
        if as_json:
            typer.echo(json.dumps(result.model_dump(mode="json"), indent=2))
        else:
            for r in result.reports:
                verdict = "[red]MOVED[/red]" if r.moved else "[green]same[/green]"
                console.print(
                    f"{r.target} ({r.kind}, {r.objective or '-'}): {verdict}  "
                    f"stored={_num(r.stored_score)} now={_num(r.current_score)} "
                    f"tolerance={r.tolerance:g}"
                )
                if r.data_changed:
                    shown = ", ".join(r.changed_tickers[:10])
                    console.print(f"  data changed for {len(r.changed_tickers)} ticker(s): {shown}")
                if r.restated_tickers:
                    shown = ", ".join(r.restated_tickers[:10])
                    console.print(f"  statements restated for: {shown}")
                if r.config_changed:
                    console.print("  the config changed since the run")
                if r.code_changed:
                    console.print("  the code changed since the run")
                if r.error:
                    console.print(f"  [yellow]{r.error}[/yellow]")
            console.print(f"{result.checked} checked, {len(result.moved)} moved")
        if result.moved:
            raise typer.Exit(code=1)

"""``stonks assistant``: run the assistant's fixed evaluation set.

Mounted by :mod:`stonks.cli`. Without ``--base-url`` the cases run against
the scripted fake model, which checks the safety code. With it they run
against that OpenAI-compatible endpoint (``--model``), which checks the
model before you switch to it. Tools are a sandbox with canned data."""

from __future__ import annotations

from typing import Any

import typer
from rich.console import Console
from rich.table import Table

app = typer.Typer(help="The in-app AI assistant", no_args_is_help=True)

_CASES = typer.Option([], "--case", help="run only these cases (repeatable)")


@app.command("eval")
def evaluate(
    base_url: str | None = typer.Option(
        None, "--base-url", help="an OpenAI-compatible endpoint; default: the scripted fake"
    ),
    model: str = typer.Option("llama3.1", "--model", help="the model name the endpoint serves"),
    case: list[str] = _CASES,
) -> None:
    """Run the eval set: reading a portfolio, resolving a ticker before a
    draft, a planted prompt injection, research only, a strategy from plain
    English, and the kill switch asking first. Exits 1 when a case fails."""
    import anyio

    from stonks.assistant.evals import CASES, fake_model_for, run_evals
    from stonks.assistant.model import OpenAICompatibleModel
    from stonks.assistant.settings import AssistantConfig

    known = {c.name for c in CASES}
    unknown = sorted(set(case) - known)
    if unknown:
        raise typer.BadParameter(f"unknown case(s): {unknown}; known: {sorted(known)}")
    config = AssistantConfig(base_url=base_url or "http://fake.local", model=model)

    def model_for(c: Any) -> Any:
        if base_url is None:
            return fake_model_for(c)
        return OpenAICompatibleModel(
            base_url, model, api_key=AssistantConfig.api_key(), timeout=config.timeout_seconds
        )

    outcomes = anyio.run(lambda: run_evals(model_for, config=config, names=tuple(case)))
    table = Table(title=f"assistant eval ({'fake' if base_url is None else model})")
    for col in ("case", "result", "tools", "why"):
        table.add_column(col)
    for o in outcomes:
        table.add_row(
            o.name,
            "[green]pass[/green]" if o.passed else "[red]fail[/red]",
            ", ".join(o.tool_calls) or "-",
            o.reason or "",
        )
    Console().print(table)
    if not all(o.passed for o in outcomes):
        raise typer.Exit(code=1)

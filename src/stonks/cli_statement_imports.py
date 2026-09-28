"""``stonks imports``: CSV statements from brokers without an API
(roadmap 23.17). Mounted by :mod:`stonks.cli`.

``preview`` shows each row as new, duplicate or skipped and writes
nothing. ``commit`` imports the new rows, ``list`` shows your imports and
``undo`` removes exactly the rows one import added. Commands act as the
owner unless ``--user`` names someone."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from stonks.cli_calendars import call, cli_principal

app = typer.Typer(help="CSV statement imports for brokers without an API", no_args_is_help=True)

_USER = typer.Option(None, "--user", help="act as this user (email or id); default: the owner")
_MAPPING = typer.Option(
    None,
    "--mapping",
    help='column mapping as JSON, e.g. {"date": "Date", "type": "Action", ...};'
    " blank guesses it from the headers",
)
_PORTFOLIO = typer.Option(None, "--portfolio", help="a portfolio made by an earlier import")
_NEW = typer.Option(None, "--new", help="import into a new portfolio with this name")
_CURRENCY = typer.Option("USD", "--currency", help="currency of a new portfolio")
_FILE = typer.Argument(..., help="the CSV file")


def _service() -> tuple[Any, Any]:
    from stonks.app.context import AppContext
    from stonks.app.statement_imports import StatementImportService
    from stonks.cli import _settings

    context = AppContext(_settings())
    with context.state() as state:
        state.migrate()
    return context, StatementImportService(context)


def _request(
    file: Path, mapping: str | None, portfolio: str | None, new: str | None, currency: str
) -> Any:
    from stonks.app.statement_imports import StatementImportRequest

    try:
        content = file.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise typer.BadParameter(str(exc), param_hint="FILE") from None
    body: dict[str, Any] = {
        "content": content,
        "filename": file.name,
        "portfolio_id": portfolio,
        "new_portfolio": new,
        "currency": currency,
    }
    if mapping is not None:
        try:
            body["mapping"] = json.loads(mapping)
        except json.JSONDecodeError as exc:
            raise typer.BadParameter(f"not JSON: {exc}", param_hint="--mapping") from None
    return call(lambda: StatementImportRequest.model_validate(body))


@app.command("preview")
def preview(
    file: Path = _FILE,
    mapping: str | None = _MAPPING,
    portfolio: str | None = _PORTFOLIO,
    new: str | None = _NEW,
    currency: str = _CURRENCY,
    user: str | None = _USER,
) -> None:
    """Show what an import would do. Writes nothing."""
    request = _request(file, mapping, portfolio, new, currency)
    context, service = _service()
    out = call(lambda: service.preview(cli_principal(context, user), request))
    if out.guessed:
        Console().print(f"guessed mapping: {out.mapping.model_dump_json(exclude_defaults=True)}")
    table = Table(title=f"{out.new} new, {out.duplicate} duplicate, {out.skipped} skipped")
    for col in ("line", "status", "kind", "day", "symbol", "quantity", "amount", "reason"):
        table.add_column(col)
    for r in out.rows:
        table.add_row(
            str(r.line),
            r.status,
            r.kind or "",
            str(r.day or ""),
            r.ticker or r.symbol or "",
            "" if r.quantity is None else f"{r.quantity:g}",
            "" if r.amount is None else f"{r.amount:g}",
            r.reason or "",
        )
    Console().print(table)
    if out.unmapped:
        Console().print(f"not covered (kept by symbol): {', '.join(out.unmapped)}")


@app.command("commit")
def commit(
    file: Path = _FILE,
    mapping: str | None = _MAPPING,
    portfolio: str | None = _PORTFOLIO,
    new: str | None = _NEW,
    currency: str = _CURRENCY,
    user: str | None = _USER,
) -> None:
    """Import the new rows of a CSV statement."""
    request = _request(file, mapping, portfolio, new, currency)
    context, service = _service()
    out = call(lambda: service.commit(cli_principal(context, user), request))
    Console().print(
        f"imported {out.id} into {out.portfolio_name or out.portfolio_id}: {out.rows_added} added,"
        f" {out.rows_duplicate} duplicate, {out.rows_skipped} skipped"
    )


@app.command("list")
def list_imports(user: str | None = _USER) -> None:
    """Your CSV imports, newest first."""
    context, service = _service()
    rows = call(lambda: service.list(cli_principal(context, user)))
    if not rows:
        Console().print("no imports")
        return
    table = Table(title="statement imports")
    for col in ("id", "portfolio", "file", "added", "duplicate", "skipped", "dates", "undone"):
        table.add_column(col)
    for r in rows:
        table.add_row(
            r.id,
            r.portfolio_name or r.portfolio_id,
            r.filename or "",
            str(r.rows_added),
            str(r.rows_duplicate),
            str(r.rows_skipped),
            f"{r.first_date or ''} to {r.last_date or ''}",
            str(r.undone_at.date()) if r.undone_at else "",
        )
    Console().print(table)


@app.command("undo")
def undo(import_id: str = typer.Argument(...), user: str | None = _USER) -> None:
    """Remove exactly the rows one import added."""
    context, service = _service()
    out = call(lambda: service.undo(cli_principal(context, user), import_id))
    Console().print(f"undid {out.id}: {out.rows_added} rows removed")

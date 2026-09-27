"""``stonks tax``: tax settings and yearly tax exports per portfolio
(roadmap 20.5).

Mounted by :mod:`stonks.cli`. Every command runs as the operator
(``service:cli``, every book) unless ``--user`` names a user, who then only
reaches their own portfolios. Not tax advice."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import typer
from rich.console import Console

app = typer.Typer(help="Tax settings and yearly tax exports", no_args_is_help=True)

_USER = typer.Option(
    None, "--user", help="act as this user (email or id); default: the operator (service:cli)"
)
_PORTFOLIO = typer.Option(
    None, "--portfolio", help="portfolio id; default: the default book (or the user's own)"
)
_YEAR = typer.Option(..., "--year", min=1900, max=2200, help="calendar year, e.g. 2025")
_OUT = typer.Option(None, "--out", help="write the CSV to this file instead of stdout")


def _service() -> tuple[Any, Any]:
    from stonks.app.context import AppContext
    from stonks.app.tax import TaxService
    from stonks.cli import _settings

    context = AppContext(_settings())
    with context.state() as state:
        state.migrate()
    return context, TaxService(context)


def _target(context: Any, user: str | None, portfolio: str | None) -> tuple[Any, str]:
    from stonks.cli_tca import _portfolio, _who

    scope = _who(context, user)
    return scope, _portfolio(context, scope, portfolio)


def _call(fn: Any) -> Any:
    from stonks.cli_tca import _call as call

    return call(fn)


def _emit(text: str, out: Path | None) -> None:
    if out is None:
        typer.echo(text, nl=False)
        return
    out.write_text(text, encoding="utf-8")
    Console(stderr=True).print(f"wrote {out}")


@app.command("gains")
def gains(
    year: int = _YEAR,
    portfolio: str | None = _PORTFOLIO,
    user: str | None = _USER,
    out: Path | None = _OUT,
) -> None:
    """Realized gains per lot disposed in the year, as CSV."""
    context, service = _service()
    _, pf = _target(context, user, portfolio)
    _emit(_call(lambda: service.gains_csv(pf, year)), out)


@app.command("dividends")
def dividends(
    year: int = _YEAR,
    portfolio: str | None = _PORTFOLIO,
    user: str | None = _USER,
    out: Path | None = _OUT,
) -> None:
    """Dividends and withholding with an ex-date in the year, as CSV."""
    context, service = _service()
    _, pf = _target(context, user, portfolio)
    _emit(_call(lambda: service.dividends_csv(pf, year)), out)


@app.command("settings")
def settings(
    portfolio: str | None = _PORTFOLIO,
    user: str | None = _USER,
    base_currency: str | None = typer.Option(None, "--base-currency", help="e.g. EUR"),
    jurisdiction: str | None = typer.Option(None, "--jurisdiction", help="us | eu | uk"),
    lot_method: str | None = typer.Option(None, "--lot-method", help="fifo | specific"),
    wash_sales: bool | None = typer.Option(
        None, "--wash-sales/--no-wash-sales", help="US wash sale adjustment"
    ),
) -> None:
    """Show a portfolio's tax settings, or change the ones you pass."""
    from pydantic import ValidationError as PydanticError

    from stonks.app.tax import TaxSettingsUpdate

    context, service = _service()
    scope, pf = _target(context, user, portfolio)
    changes = {
        "base_currency": base_currency.upper() if base_currency else None,
        "jurisdiction": jurisdiction,
        "lot_method": lot_method,
        "wash_sales": wash_sales,
    }
    if any(v is not None for v in changes.values()):
        try:
            body = TaxSettingsUpdate(**changes)  # type: ignore[arg-type]
        except PydanticError as exc:
            raise typer.BadParameter(str(exc)) from None
        view = _call(lambda: service.update_settings(scope, pf, body))
    else:
        view = _call(lambda: service.settings(pf))
    Console().print(
        f"{view.portfolio_id}: base {view.base_currency}, {view.jurisdiction}, "
        f"lots {view.lot_method}, wash sales {'on' if view.wash_sales else 'off'}"
    )

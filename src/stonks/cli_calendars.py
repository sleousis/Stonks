"""``stonks calendars``: earnings, ex-dividend dates, economic releases and
news (roadmap 20.7). Mounted by :mod:`stonks.cli`.

Reads run as the owner unless ``--user`` names someone, who then sees
their own holdings and watchlists. ``refresh`` writes the lake, so it
needs the lake free (stop ``stonks serve`` or use the API)."""

from __future__ import annotations

from datetime import date
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

app = typer.Typer(help="Event calendars and news (roadmap 20.7)", no_args_is_help=True)

_USER = typer.Option(None, "--user", help="act as this user (email or id); default: the owner")
_SCOPE = typer.Option("holdings", "--scope", help="holdings | watchlists | tickers | all")
_TICKERS = typer.Option(None, "--tickers", help="comma-separated instrument ids (scope tickers)")
_WATCHLIST = typer.Option(None, "--watchlist", help="one watchlist id (scope watchlists)")
_PORTFOLIO = typer.Option(None, "--portfolio", help="one portfolio id (scope holdings)")


def _day(value: str | None, flag: str) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise typer.BadParameter("expected YYYY-MM-DD", param_hint=flag) from None


def _split(value: str | None) -> list[str]:
    return [t.strip() for t in (value or "").split(",") if t.strip()]


def cli_principal(context: Any, user: str | None) -> Any:
    """The owner as an admin, or the named user with their role's rights."""
    from stonks.accounts import DEFAULT_OWNER_ID, NotFound, Role, UserRepository
    from stonks.auth import Principal
    from stonks.auth.principal import ROLE_SCOPES

    user_id, role = DEFAULT_OWNER_ID, Role.ADMIN
    if user is not None:
        with context.state() as state:
            users = UserRepository(state)
            try:
                found = users.get_by_email(user) if "@" in user else users.get(user)
            except NotFound:
                raise typer.BadParameter(f"no user {user!r}", param_hint="--user") from None
        user_id, role = found.id, found.role
    return Principal.create(
        user_id=user_id,
        kind="human",
        role=role,
        scopes=ROLE_SCOPES[role],
        mfa_fresh=False,
        via="cli",
    )


def call(fn: Any) -> Any:
    """Run a service call, turning its errors into a usage error."""
    from pydantic import ValidationError as PydanticValidationError

    from stonks.app.errors import AppError

    try:
        return fn()
    except AppError as exc:
        raise typer.BadParameter(str(exc)) from None
    except PydanticValidationError as exc:
        raise typer.BadParameter(exc.errors()[0]["msg"]) from None


def _service() -> tuple[Any, Any]:
    from stonks.app.calendars import CalendarService
    from stonks.app.context import AppContext
    from stonks.cli import _settings

    context = AppContext(_settings())
    with context.state() as state:
        state.migrate()
    return context, CalendarService(context)


def _filter(scope: str, tickers: str | None, watchlist: str | None, portfolio: str | None) -> Any:
    from stonks.app.calendars import CalendarFilter

    return call(
        lambda: CalendarFilter.model_validate(
            {
                "scope": scope,
                "tickers": _split(tickers),
                "watchlist_id": watchlist,
                "portfolio_id": portfolio,
            }
        )
    )


def _num(value: float | None) -> str:
    return "-" if value is None else f"{value:g}"


@app.command("show")
def show(
    scope: str = _SCOPE,
    tickers: str | None = _TICKERS,
    watchlist: str | None = _WATCHLIST,
    portfolio: str | None = _PORTFOLIO,
    start: str | None = typer.Option(None, "--start", help="first day (default today)"),
    end: str | None = typer.Option(None, "--end", help="last day (default two weeks on)"),
    countries: str | None = typer.Option(
        None, "--countries", help="economic events of these ISO alpha-2 codes"
    ),
    user: str | None = _USER,
) -> None:
    """Earnings, ex-dividend dates and economic releases in a window."""
    context, service = _service()
    principal = cli_principal(context, user)
    flt = _filter(scope, tickers, watchlist, portfolio)
    view = call(
        lambda: service.calendar(
            principal,
            flt,
            start=_day(start, "--start"),
            end=_day(end, "--end"),
            countries=[c.upper() for c in _split(countries)] or None,
        )
    )
    console = Console()
    earnings = Table(title=f"earnings {view.start} to {view.end}")
    for col in ("date", "ticker", "name", "when", "EPS est.", "EPS actual"):
        earnings.add_column(col)
    for e in view.earnings:
        earnings.add_row(
            str(e.report_date),
            e.ticker,
            e.name or "",
            e.before_after_market or "",
            _num(e.eps_estimate),
            _num(e.eps_actual),
        )
    console.print(earnings)
    dividends = Table(title="ex-dividend")
    for col in ("ex date", "ticker", "amount", "pay date"):
        dividends.add_column(col)
    for d in view.dividends:
        dividends.add_row(str(d.ex_date), d.ticker, _num(d.amount), str(d.pay_date or ""))
    console.print(dividends)
    economic = Table(title="economic releases")
    for col in ("time (UTC)", "country", "event", "estimate", "previous", "actual"):
        economic.add_column(col)
    for x in view.economic:
        economic.add_row(
            x.event_time.strftime("%Y-%m-%d %H:%M"),
            x.country,
            x.event_type,
            _num(x.estimate),
            _num(x.previous),
            _num(x.actual),
        )
    console.print(economic)
    if view.truncated:
        console.print("[yellow]truncated: narrow the scope or the window[/yellow]")


@app.command("news")
def news(
    scope: str = _SCOPE,
    tickers: str | None = _TICKERS,
    watchlist: str | None = _WATCHLIST,
    portfolio: str | None = _PORTFOLIO,
    limit: int = typer.Option(20, "--limit", min=1, max=200),
    user: str | None = _USER,
) -> None:
    """The newest articles on the scope's tickers."""
    context, service = _service()
    principal = cli_principal(context, user)
    flt = _filter(scope, tickers, watchlist, portfolio)
    view = call(lambda: service.news(principal, flt, limit=limit))
    table = Table(title=f"news ({len(view.tickers)} tickers)")
    for col in ("published", "ticker", "sentiment", "title"):
        table.add_column(col)
    for n in view.items:
        table.add_row(
            n.published_at.strftime("%Y-%m-%d %H:%M"), n.ticker, _num(n.sentiment), n.title
        )
    Console().print(table)


@app.command("earnings-check")
def earnings_check(
    tickers: str = typer.Argument(..., help="comma-separated instrument ids"),
    user: str | None = _USER,
) -> None:
    """Which tickers report earnings before the next open of their market."""
    context, service = _service()
    principal = cli_principal(context, user)
    view = call(lambda: service.earnings_warnings(principal, _split(tickers)))
    if not view.warnings:
        Console().print("no earnings before the next open")
        return
    for w in view.warnings:
        when = w.before_after_market or "time not given"
        Console().print(
            f"[yellow]{w.ticker}[/yellow]: reports {w.report_date} ({when}), "
            f"next open {w.next_open:%Y-%m-%d %H:%M} UTC"
        )


@app.command("refresh")
def refresh(
    source: str = typer.Option("eodhd", "--source", help="data source id"),
    start: str | None = typer.Option(None, "--start", help="first day (default a week ago)"),
    end: str | None = typer.Option(None, "--end", help="last day (default five weeks on)"),
    tickers: str | None = typer.Option(None, "--tickers", help="only these tickers"),
    countries: str | None = typer.Option(None, "--countries", help="ISO alpha-2 codes"),
    alerts: bool = typer.Option(True, "--alerts/--no-alerts", help="send event notifications"),
) -> None:
    """Pull the calendars into the lake, then send the upcoming-event
    notifications (the scheduler's calendars_refresh job)."""
    from stonks.app.calendars import CalendarRefreshRequest

    context, service = _service()
    request = call(
        lambda: CalendarRefreshRequest.model_validate(
            {
                "source": source,
                "start": _day(start, "--start"),
                "end": _day(end, "--end"),
                "tickers": _split(tickers) or None,
                "countries": [c.upper() for c in _split(countries)] or None,
                "alerts": alerts,
            }
        )
    )
    with context.lake() as lake:
        lake.migrate()
    view = call(lambda: service.refresh(request))
    Console().print(
        f"run {view.run_id}: {view.status}, {view.calendars_ok} ok, "
        f"{view.calendars_failed} failed {view.failed or ''}"
    )
    if view.alerts is not None:
        Console().print(f"event alerts sent: {view.alerts.sent}")

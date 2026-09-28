"""``stonks journal``: the round-trip journal (roadmap 23.3).

Mounted by :mod:`stonks.cli`. Every command runs as the operator
(``service:cli``, every book) unless ``--user`` names a user, who then only
sees their own portfolios and playbooks."""

from __future__ import annotations

import json
from typing import Any

import typer
from rich.table import Table

from stonks.cli_tca import _PORTFOLIO, _USER, _call, _day, _portfolio, _who, console

app = typer.Typer(help="The round-trip journal: trades, review, P&L calendar", no_args_is_help=True)

_SINCE = typer.Option(None, "--since", help="closed on or after this day (YYYY-MM-DD)")
_UNTIL = typer.Option(None, "--until", help="closed on or before this day (YYYY-MM-DD)")
_JSON = typer.Option(False, "--json", help="print JSON")
_TAGS = typer.Option([], "--tag", help="a tag (repeat for more)")
_MISTAKES = typer.Option([], "--mistake", help="a mistake (repeat for more)")


def _service() -> tuple[Any, Any]:
    from stonks.app.context import AppContext
    from stonks.app.journal import JournalService
    from stonks.cli import _settings

    context = AppContext(_settings())
    with context.state() as state:
        state.migrate()
    return context, JournalService(context)


def _num(value: float | None, digits: int = 2) -> str:
    return "-" if value is None else f"{value:,.{digits}f}"


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{value * 100:.1f}%"


def _dump(model: Any) -> None:
    typer.echo(json.dumps(model.model_dump(mode="json"), indent=2))


@app.command("trades")
def trades(
    status: str = typer.Option("all", "--status", help="all | open | closed"),
    sleeve: str | None = typer.Option(None, "--sleeve", help="one strategy id, or manual"),
    ticker: str | None = typer.Option(None, "--ticker", help="one ticker"),
    tag: str | None = typer.Option(None, "--tag", help="trades with this tag"),
    since: str | None = _SINCE,
    until: str | None = _UNTIL,
    limit: int = typer.Option(50, "--limit", min=1, max=500),
    portfolio: str | None = _PORTFOLIO,
    user: str | None = _USER,
    as_json: bool = _JSON,
) -> None:
    """Round trips built from fills: P&L, holding time, excursions, R."""
    from stonks.app.journal import TradeFilter

    if status not in ("all", "open", "closed"):
        raise typer.BadParameter("all, open or closed", param_hint="--status")
    context, service = _service()
    pf = _portfolio(context, _who(context, user), portfolio)
    filters = TradeFilter(
        status=status,  # type: ignore[arg-type]
        sleeve=sleeve,
        ticker=ticker,
        tag=tag,
        since=_day(since, "--since"),
        until=_day(until, "--until"),
    )
    page = _call(lambda: service.trades(pf, filters, limit=limit))
    if as_json:
        _dump(page)
        return
    if not page.items:
        console.print("no trades yet")
        return
    table = Table(title=f"round trips ({pf}), {page.total} legs")
    for col in (
        "leg",
        "ticker",
        "side",
        "sleeve",
        "entry",
        "exit",
        "days",
        "P&L",
        "R",
        "MAE",
        "MFE",
        "exit eff.",
        "tags",
    ):
        table.add_column(col)
    for t in page.items:
        table.add_row(
            t.leg_id,
            t.ticker,
            t.side,
            t.sleeve,
            f"{t.entry_at:%Y-%m-%d} @ {t.entry_price:,.2f}",
            "open" if t.is_open else f"{t.exit_at:%Y-%m-%d} @ {t.exit_price:,.2f}",
            _num(t.holding_days, 1),
            _num(t.pnl),
            _num(t.r_multiple),
            _pct(t.mae_pct),
            _pct(t.mfe_pct),
            _pct(t.exit_efficiency),
            ", ".join(t.tags + [f"!{m}" for m in t.mistakes]),
        )
    console.print(table)


@app.command("show")
def show(
    trade_id: int = typer.Argument(..., help="the trade id (the opening fill's id)"),
    portfolio: str | None = _PORTFOLIO,
    user: str | None = _USER,
) -> None:
    """One trade with every leg and its review, as JSON."""
    context, service = _service()
    pf = _portfolio(context, _who(context, user), portfolio)
    _dump(_call(lambda: service.trade(pf, trade_id)))


@app.command("review")
def review(
    trade_id: int = typer.Argument(..., help="the trade id (the opening fill's id)"),
    tag: list[str] = _TAGS,
    mistake: list[str] = _MISTAKES,
    playbook: str | None = typer.Option(None, "--playbook", help="a playbook id"),
    followed: bool | None = typer.Option(
        None, "--followed/--broke", help="whether you followed the plan"
    ),
    text: str | None = typer.Option(None, "--text", help="a short review"),
    portfolio: str | None = _PORTFOLIO,
    user: str | None = _USER,
) -> None:
    """Set a trade's tags, mistakes, playbook and plan flag (replaces them)."""
    from stonks.app.journal import AnnotationRequest

    context, service = _service()
    scope = _who(context, user)
    pf = _portfolio(context, scope, portfolio)
    request = AnnotationRequest(
        tags=tag, mistakes=mistake, playbook_id=playbook, followed_plan=followed, review=text
    )
    saved = _call(lambda: service.annotate(scope, pf, trade_id, request))
    console.print(f"trade {saved.trade_id} reviewed: tags {saved.tags}, mistakes {saved.mistakes}")


@app.command("calendar")
def calendar(
    period: str = typer.Option("month", "--by", help="day | week | month"),
    since: str | None = _SINCE,
    until: str | None = _UNTIL,
    portfolio: str | None = _PORTFOLIO,
    user: str | None = _USER,
    as_json: bool = _JSON,
) -> None:
    """Realised P&L by exit day, week or month, in the base currency."""
    if period not in ("day", "week", "month"):
        raise typer.BadParameter("day, week or month", param_hint="--by")
    context, service = _service()
    pf = _portfolio(context, _who(context, user), portfolio)
    cal = _call(
        lambda: service.calendar(pf, since=_day(since, "--since"), until=_day(until, "--until"))
    )
    if as_json:
        _dump(cal)
        return
    buckets = {"day": cal.days, "week": cal.weeks, "month": cal.months}[period]
    if not buckets:
        console.print("no closed trades yet")
        return
    table = Table(title=f"P&L by {period} ({pf}, {cal.base_currency})")
    for col in (period, "P&L", "trades", "wins"):
        table.add_column(col)
    for b in buckets:
        table.add_row(b.key, _num(b.pnl), str(b.trades), str(b.wins))
    console.print(table)
    console.print(f"total {_num(cal.total)} over {cal.trades} closed legs")
    if cal.unconverted:
        console.print(f"{cal.unconverted} legs left out: no FX rate for {cal.fx_missing}")


@app.command("breakdown")
def breakdown(
    by: str = typer.Option(
        "all",
        "--by",
        help="all | sleeve | origin | ticker | side | exit_trigger | tag | mistake | playbook | plan",
    ),
    since: str | None = _SINCE,
    until: str | None = _UNTIL,
    portfolio: str | None = _PORTFOLIO,
    user: str | None = _USER,
    as_json: bool = _JSON,
) -> None:
    """Win rate, P&L, profit factor, average R and exit efficiency by group."""
    from stonks.app.journal import BREAKDOWN_BYS

    if by not in BREAKDOWN_BYS:
        raise typer.BadParameter(f"one of {', '.join(BREAKDOWN_BYS)}", param_hint="--by")
    context, service = _service()
    pf = _portfolio(context, _who(context, user), portfolio)
    view = _call(
        lambda: service.breakdown(
            pf,
            by=by,  # type: ignore[arg-type]
            since=_day(since, "--since"),
            until=_day(until, "--until"),
        )
    )
    if as_json:
        _dump(view)
        return
    if not view.groups:
        console.print("no trades yet")
        return
    table = Table(title=f"results by {by} ({pf}, {view.base_currency})")
    for col in (
        "group",
        "trades",
        "open",
        "win rate",
        "P&L",
        "profit factor",
        "avg R",
        "exit eff.",
    ):
        table.add_column(col)
    for g in view.groups:
        table.add_row(
            g.key,
            str(g.trades),
            str(g.open),
            _pct(g.win_rate),
            _num(g.pnl),
            _num(g.profit_factor),
            _num(g.avg_r),
            _pct(g.avg_exit_efficiency),
        )
    console.print(table)


@app.command("playbooks")
def playbooks(
    archived: bool = typer.Option(False, "--archived", help="also list archived playbooks"),
    user: str | None = _USER,
) -> None:
    """Your playbooks."""
    context, service = _service()
    found = _call(lambda: service.playbooks(_who(context, user), include_archived=archived))
    if not found:
        console.print("no playbooks yet")
        return
    table = Table(title="playbooks")
    for col in ("id", "name", "archived", "rules"):
        table.add_column(col)
    for p in found:
        table.add_row(p.id, p.name, "yes" if p.archived else "", p.description or "")
    console.print(table)


@app.command("playbook-add")
def playbook_add(
    name: str = typer.Argument(..., help="a short name, such as breakout"),
    description: str | None = typer.Option(None, "--rules", help="the rules you mean to follow"),
    user: str | None = _USER,
) -> None:
    """Add a playbook."""
    from stonks.app.journal import PlaybookCreate

    context, service = _service()
    made = _call(
        lambda: service.create_playbook(
            _who(context, user), PlaybookCreate(name=name, description=description)
        )
    )
    console.print(f"playbook {made.id} added: {made.name}")


@app.command("playbook-edit")
def playbook_edit(
    playbook_id: str = typer.Argument(..., help="the playbook id"),
    name: str | None = typer.Option(None, "--name", help="a new name"),
    description: str | None = typer.Option(None, "--rules", help="new rules"),
    archived: bool | None = typer.Option(None, "--archive/--restore", help="archive or restore"),
    user: str | None = _USER,
) -> None:
    """Rename a playbook, change its rules, or archive it."""
    from stonks.app.journal import PlaybookUpdate

    context, service = _service()
    request = PlaybookUpdate(name=name, description=description, archived=archived)
    updated = _call(lambda: service.update_playbook(_who(context, user), playbook_id, request))
    console.print(
        f"playbook {updated.id}: {updated.name}{' (archived)' if updated.archived else ''}"
    )


@app.command("labels")
def labels(portfolio: str | None = _PORTFOLIO, user: str | None = _USER) -> None:
    """The tags and mistakes already used in the portfolio."""
    context, service = _service()
    pf = _portfolio(context, _who(context, user), portfolio)
    view = _call(lambda: service.labels(pf))
    console.print(f"tags: {', '.join(view.tags) or '-'}")
    console.print(f"mistakes: {', '.join(view.mistakes) or '-'}")

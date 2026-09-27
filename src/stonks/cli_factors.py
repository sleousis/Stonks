"""``stonks factors``: the factor library, formula checks, values at a date,
tear sheets and dataset exports (roadmap 22.2, 22.3, 22.8).

Mounted by :mod:`stonks.cli`. Research only: the commands read the lake and
write nothing but the files you name and the panel cache."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

app = typer.Typer(
    help="Factors: the library, formula checks, values, tear sheets and datasets",
    no_args_is_help=True,
)


class _LazyConsole:
    """A fresh rich console per call, so the terminal width is read when a
    command prints, not at import."""

    def print(self, *args: Any, **kwargs: Any) -> None:
        Console().print(*args, **kwargs)


console = _LazyConsole()

_TICKERS = typer.Option(None, "--tickers", help="comma-separated tickers")
_UNIVERSE = typer.Option(None, "--universe-id", help="a stored universe (point-in-time members)")


def _day(value: str, flag: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise typer.BadParameter("expected YYYY-MM-DD", param_hint=flag) from None


def _ints(value: str, flag: str) -> list[int]:
    try:
        return [int(v) for v in value.split(",") if v.strip()]
    except ValueError:
        raise typer.BadParameter(
            "expected comma-separated whole numbers", param_hint=flag
        ) from None


def _service() -> Any:
    from stonks.app.context import AppContext
    from stonks.app.factors import FactorService
    from stonks.cli import _settings

    return FactorService(AppContext(_settings()))


def _call(fn: Any) -> Any:
    from pydantic import ValidationError as PydanticError

    from stonks.app.errors import AppError

    try:
        return fn()
    except AppError as exc:
        raise typer.BadParameter(str(exc)) from None
    except PydanticError as exc:
        raise typer.BadParameter(exc.errors()[0]["msg"]) from None


def _universe(tickers: str | None, universe_id: str | None) -> dict[str, Any]:
    from stonks.cli import _parse_tickers

    names = _parse_tickers(tickers)
    return {"universe": names or None, "universe_id": universe_id}


def _fmt(x: float | None, spec: str = "{:+.4f}") -> str:
    return "-" if x is None else spec.format(x)


@app.command("list")
def list_factors(
    family: str | None = typer.Option(None, "--family", help="e.g. momentum, value, kbar"),
    set_name: str | None = typer.Option(None, "--set", help="alpha158, classic or fundamentals"),
    kind: str | None = typer.Option(None, "--kind", help="expression or fundamental"),
) -> None:
    """Every library factor, or one family, set or kind."""
    view = _call(lambda: _service().catalog(family=family, set=set_name, kind=kind))
    table = Table(title=f"{len(view.factors)} factors")
    for col in ("id", "set", "family", "dir", "warm-up", "description"):
        table.add_column(col)
    for f in view.factors:
        table.add_row(
            f.id,
            f.set or "",
            f.family,
            "+1" if f.direction > 0 else "-1",
            str(f.lookback_bars),
            f.description,
        )
    console.print(table)
    console.print("sets: " + ", ".join(f"{s.name} ({s.count})" for s in view.sets))


@app.command("show")
def show(factor_id: str = typer.Argument(..., help="a library factor id")) -> None:
    """One factor: formula, family, direction, hypothesis and warm-up."""
    f = _call(lambda: _service().get(factor_id))
    console.print(f"[bold]{f.id}[/bold] ({f.set}, {f.family}, {f.kind})")
    console.print(f.description)
    if f.expression:
        console.print(f"formula: {f.expression}")
    console.print(
        f"direction: {'+1' if f.direction > 0 else '-1'}, warm-up: {f.lookback_bars} bars"
    )
    console.print(f"asset classes: {', '.join(f.asset_classes)}")
    if f.hypothesis:
        console.print(f"hypothesis: {f.hypothesis}")


@app.command("check")
def check(
    expression: str = typer.Argument(..., help="a formula, e.g. '$close/Ref($close,20)-1'"),
) -> None:
    """Check a formula: it must parse and be point in time."""
    from stonks.app.factors import ExpressionCheckRequest

    view = _call(lambda: _service().check_expression(ExpressionCheckRequest(expression=expression)))
    if not view.ok:
        console.print(f"[red]not a valid factor:[/red] {view.error}")
        raise typer.Exit(code=1)
    console.print(f"ok: {view.canonical} (warm-up {view.lookback_bars} bars)")


@app.command("values")
def values(
    factor: str = typer.Argument(..., help="a library factor id or a formula"),
    as_of: str = typer.Option(..., "--as-of", help="YYYY-MM-DD"),
    tickers: str | None = _TICKERS,
    universe_id: str | None = _UNIVERSE,
    top: int = typer.Option(20, "--top", min=1, help="rows to print"),
    as_json: bool = typer.Option(False, "--json", help="print JSON instead of a table"),
) -> None:
    """Factor values across a universe at a date, best first."""
    from stonks.app.factors import FactorValuesRequest

    def run() -> Any:
        request = FactorValuesRequest(
            factor=factor, as_of=_day(as_of, "--as-of"), **_universe(tickers, universe_id)
        )
        return _service().values(request)

    view = _call(run)
    if as_json:
        typer.echo(json.dumps(view.model_dump(mode="json"), indent=2))
        return
    table = Table(title=f"{view.factor_id} on {view.as_of}")
    for col in ("rank", "ticker", "value"):
        table.add_column(col)
    for v in view.values[:top]:
        table.add_row(str(v.rank), v.ticker, f"{v.value:.6g}")
    console.print(table)
    if view.missing:
        console.print(f"no value: {', '.join(view.missing)}")


@app.command("tearsheet")
def tearsheet(
    factor: str = typer.Argument(..., help="a library factor id or a formula"),
    start: str = typer.Option(..., "--start", help="YYYY-MM-DD"),
    end: str = typer.Option(..., "--end", help="YYYY-MM-DD"),
    tickers: str | None = _TICKERS,
    universe_id: str | None = _UNIVERSE,
    interval: str = typer.Option("1d", "--interval"),
    horizons: str = typer.Option("1,5,21", "--horizons", help="forward-return horizons in bars"),
    every_bars: int = typer.Option(5, "--every-bars", min=1, help="sample every N bars"),
    quantiles: int = typer.Option(5, "--quantiles", min=2, max=20),
    json_path: str | None = typer.Option(None, "--json", help="write the tear sheet as JSON"),
    html_path: str | None = typer.Option(None, "--html", help="write the tear sheet as HTML"),
) -> None:
    """A factor tear sheet: IC by horizon, sector, asset class and size,
    returns per quantile, alpha and beta, monthly IC and turnover."""
    from stonks.app.factors import FactorTearSheetRequest
    from stonks.reporting.factors import render_factor_page

    def run() -> Any:
        request = FactorTearSheetRequest(
            factor=factor,
            start=_day(start, "--start"),
            end=_day(end, "--end"),
            interval=interval,
            horizons=_ints(horizons, "--horizons"),
            every_bars=every_bars,
            n_quantiles=quantiles,
            **_universe(tickers, universe_id),
        )
        return _service().compute_tearsheet(request)

    sheet = _call(run)
    if json_path is not None:
        Path(json_path).write_text(json.dumps(sheet.to_dict(), indent=2), encoding="utf-8")
    if html_path is not None:
        Path(html_path).write_text(render_factor_page(sheet), encoding="utf-8")
    _print_sheet(sheet)


def _print_sheet(sheet: Any) -> None:
    head = (
        f"{sheet.factor['id']}: {sheet.window[0]} to {sheet.window[1]}, {sheet.n_tickers} tickers"
    )
    if sheet.status != "ok":
        console.print(f"{head}\n{sheet.status}: {sheet.note}")
        return
    table = Table(title=head)
    for col in ("horizon", "dates", "mean IC", "ICIR", "t (HAC)", "top-bottom"):
        table.add_column(col)
    for h in sheet.horizons:
        table.add_row(
            str(h.horizon),
            str(h.n_dates),
            _fmt(h.mean_ic),
            _fmt(h.icir, "{:+.3f}"),
            _fmt(h.t_stat_hac, "{:+.2f}"),
            _fmt(h.spread_mean, "{:+.3%}"),
        )
    console.print(table)
    ab = sheet.alpha_beta
    console.print(
        f"alpha {_fmt(ab.alpha_annual, '{:+.2%}')} a year (t {_fmt(ab.alpha_t, '{:+.2f}')}), "
        f"beta {_fmt(ab.beta, '{:+.2f}')}, coverage {_fmt(sheet.coverage, '{:.0%}')}, "
        f"top-bucket turnover {_fmt(sheet.top_quantile_turnover, '{:.2f}')}"
    )
    for key, groups in sheet.ic_by_group.items():
        parts = ", ".join(f"{g.group} {_fmt(g.mean_ic, '{:+.3f}')}" for g in groups)
        console.print(f"IC by {key.replace('_', ' ')}: {parts}")


@app.command("dataset")
def dataset(
    factors: str = typer.Argument(..., help="sets, ids and formulas, e.g. 'alpha158,mom_12_1'"),
    start: str = typer.Option(..., "--start", help="YYYY-MM-DD"),
    end: str = typer.Option(..., "--end", help="YYYY-MM-DD"),
    out: str = typer.Option(..., "--out", help="a .parquet or .csv file"),
    tickers: str | None = _TICKERS,
    universe_id: str | None = _UNIVERSE,
    interval: str = typer.Option("1d", "--interval"),
    label_horizon: int = typer.Option(1, "--label-horizon", min=1, help="bars of the label"),
    no_label: bool = typer.Option(False, "--no-label", help="leave the label out"),
) -> None:
    """Export factor values and a next-open label per date and ticker, the
    table a model trains on."""
    from stonks.app.factors import FactorDatasetRequest

    target = Path(out)
    if target.suffix.lower() not in (".parquet", ".csv"):
        raise typer.BadParameter("use a .parquet or .csv file", param_hint="--out")

    def run() -> Any:
        request = FactorDatasetRequest(
            factors=factors,
            start=_day(start, "--start"),
            end=_day(end, "--end"),
            interval=interval,
            label_horizon=None if no_label else label_horizon,
            **_universe(tickers, universe_id),
        )
        return _service().dataset(request)

    from stonks.factors.dataset import write_dataset

    frame = _call(run)
    write_dataset(frame, target)
    console.print(f"wrote {len(frame)} rows x {len(frame.columns)} columns to {target}")

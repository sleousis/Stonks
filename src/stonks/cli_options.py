"""``stonks options``: option chains, the strategy catalog and the options
backtest (roadmap Phase 17). Research only: nothing here trades.

Mounted by :mod:`stonks.cli`."""

from __future__ import annotations

from datetime import date
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

app = typer.Typer(
    help="Options research: chains, strategies, backtests (Phase 17)", no_args_is_help=True
)


def _console() -> Console:
    return Console()


def _day(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


def _tickers(raw: str) -> list[str]:
    return [t.strip() for t in raw.split(",") if t.strip()]


@app.command("ingest")
def ingest(
    underlyings: str = typer.Option(..., "--underlyings", help="comma-separated, e.g. AAPL.US"),
    since: str | None = typer.Option(None, "--since", help="first day (YYYY-MM-DD)"),
    until: str | None = typer.Option(None, "--until", help="last day (YYYY-MM-DD)"),
    source_id: str = typer.Option("eodhd", "--source", help="options data source"),
    max_expiry_days: int | None = typer.Option(365, "--max-expiry-days"),
    strike_band: float | None = typer.Option(
        0.3, "--strike-band", help="keep strikes within this share of spot"
    ),
) -> None:
    """Fetch end-of-day option chains into the lake (EODHD needs its
    Marketplace options subscription)."""
    from stonks.cli import _build_source, _open_lake, _settings
    from stonks.options.ingest import ingest_option_quotes

    settings = _settings()
    source = _build_source(settings, source_id)
    with _open_lake(settings.lake.path) as lake:
        lake.migrate()
        result = ingest_option_quotes(
            source,
            lake,
            _tickers(underlyings),
            since=_day(since),
            until=_day(until),
            max_expiry_days=max_expiry_days,
            strike_band=strike_band,
        )
    _console().print(
        f"options ingest #{result.run_id}: {result.status}, {result.rows} quotes, "
        f"ok {len(result.ok)}, failed {len(result.failed)}"
    )
    for underlying, error in result.failed.items():
        _console().print(f"  [red]{underlying}[/red] {error}")


@app.command("chain")
def chain(
    underlying: str = typer.Argument(..., help="e.g. AAPL.US"),
    as_of: str = typer.Option(..., "--as-of", help="YYYY-MM-DD"),
    expiry: str | None = typer.Option(None, "--expiry", help="only this expiry"),
) -> None:
    """One day's chain with our implied vol and Greeks."""
    from stonks.cli import _open_lake, _settings
    from stonks.options.analytics import analyze
    from stonks.options.store import OptionStore

    settings = _settings()
    with _open_lake(settings.lake.path) as lake:
        snap = OptionStore(lake).chain(underlying, date.fromisoformat(as_of))
    want = _day(expiry)
    table = Table(title=f"{underlying} {as_of} (spot {snap.spot})")
    for col in ("contract", "bid", "ask", "iv", "delta", "gamma", "vega/pt", "theta/day"):
        table.add_column(col)
    for q in snap:
        if want is not None and q.contract.expiry != want:
            continue
        a = analyze(q, snap.spot)
        g = a.greeks

        def f(x: Any, fmt: str = ".2f") -> str:
            return "" if x is None else format(x, fmt)

        table.add_row(
            q.contract_id,
            f(q.bid),
            f(q.ask),
            f(a.iv, ".3f"),
            f(g.delta if g else None, ".3f"),
            f(g.gamma if g else None, ".4f"),
            f(g.vega_per_point if g else None, ".3f"),
            f(g.theta_per_day if g else None, ".3f"),
        )
    _console().print(table)


@app.command("strategies")
def strategies() -> None:
    """The options strategy catalog with each hypothesis."""
    from stonks.options.strategies import option_strategy_catalog

    for sid, cls in option_strategy_catalog().items():
        _console().print(f"[bold]{sid}[/bold] ({', '.join(cls.structures)})")
        _console().print(f"  {cls.hypothesis}")


@app.command("backtest")
def backtest(
    strategy: str = typer.Argument(..., help="an id from `stonks options strategies`"),
    underlyings: str = typer.Option(..., "--underlyings"),
    start: str = typer.Option(..., "--start"),
    end: str = typer.Option(..., "--end"),
    cash: float = typer.Option(100_000.0, "--cash"),
    validate: bool = typer.Option(False, "--validate", help="also run the survival checks"),
    trials: int = typer.Option(1, "--trials", help="trials run so far, for the deflated Sharpe"),
) -> None:
    """Backtest an options strategy on the lake's chains."""
    from stonks.backtest.options_engine import (
        OptionMarketData,
        OptionsBacktestConfig,
        OptionsBacktester,
    )
    from stonks.cli import _open_lake, _settings
    from stonks.options.strategies import resolve_option_strategy
    from stonks.options.validation import OptionValidationSettings, validate_option_strategy

    try:
        cls = resolve_option_strategy(strategy)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    settings = _settings()
    tickers = _tickers(underlyings)
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    with _open_lake(settings.lake.path) as lake:
        data = OptionMarketData.from_lake(lake, tickers, first, last)
    config = OptionsBacktestConfig(start=first, end=last, underlyings=tickers, initial_cash=cash)
    result = OptionsBacktester(cls(), data, config).run()
    r = result.report
    _console().print(
        f"{strategy}: {r.n_bars} days, return {r.final_return:.2%}, Sharpe {r.sharpe:.2f} "
        f"(1 trial), max drawdown {r.max_drawdown:.2%}, {result.n_fills} fills, "
        f"{len(result.rejections)} rejected"
    )
    if validate:
        for report in validate_option_strategy(
            cls, None, data, config, OptionValidationSettings(n_trials=trials)
        ):
            mark = "[green]pass[/green]" if report.passed else "[red]fail[/red]"
            metrics = ", ".join(f"{k} {v:.3g}" for k, v in report.metrics.items())
            _console().print(f"  {report.test_id}: {mark} ({metrics})")

"""Typer CLI entrypoint — ``stonks`` command. Run ``stonks --help`` for the subcommand surface.

To browse the lake interactively, run ``uv run duckdb -ui data/lake.duckdb``
(requires the standalone DuckDB CLI on PATH: https://install.duckdb.org).
"""

from __future__ import annotations

import importlib
from datetime import date
from pathlib import Path
from typing import cast, get_args

import typer
from rich.console import Console
from rich.table import Table

from stonks.config import Settings, load_settings
from stonks.core.types import AssetClass
from stonks.ingest.pipeline import IngestPipeline, IngestRunResult
from stonks.ingest.sources.base import DataSource
from stonks.ingest.sources.eodhd import (
    EodhdDataSource,
    EodhdFreeTierError,
    classify_asset_class,
    eodhd_exchange_for_asset_class,
)
from stonks.logging import configure_logging, get_logger
from stonks.production.tick import TickSettings, run_tick
from stonks.registry.store import StrategyRegistry
from stonks.store.lake import DuckDBLake
from stonks.store.state import SqliteState

_ASSET_CLASS_CHOICES: tuple[str, ...] = get_args(AssetClass)


def _validate_asset_class(value: str | None) -> str | None:
    if value is None or value in _ASSET_CLASS_CHOICES:
        return value
    raise typer.BadParameter(
        f"--asset-class must be one of {list(_ASSET_CLASS_CHOICES)}, got {value!r}"
    )


def _strategy_applicable_classes(class_path: str) -> tuple[AssetClass, ...]:
    """Read ``applicable_asset_classes`` off a registered strategy's class
    object without instantiating it. Falls back to ``("equity",)`` when
    the import fails (registry has many entries; one broken module
    shouldn't fail the whole flow)."""
    try:
        module_name, cls_name = class_path.split(":", 1)
        module = importlib.import_module(module_name)
        cls = getattr(module, cls_name)
        return tuple(getattr(cls, "applicable_asset_classes", ("equity",)))
    except Exception:
        return ("equity",)


app = typer.Typer(add_completion=False, help="Stonks CLI")
db_app = typer.Typer(help="Database / lake operations")
ingest_app = typer.Typer(help="Data ingestion")
registry_app = typer.Typer(help="Strategy registry operations")
app.add_typer(db_app, name="db")
app.add_typer(ingest_app, name="ingest")
app.add_typer(registry_app, name="registry")

console = Console()


# ---- helpers ----------------------------------------------------------------


def _settings() -> Settings:
    # CLI is the right place to materialize .env into the process env;
    # load_settings itself stays pure so tests can monkeypatch freely.
    from dotenv import load_dotenv

    load_dotenv(override=False)
    settings = load_settings()
    configure_logging(level=settings.logging.level)
    return settings


def _build_source(settings: Settings) -> DataSource:
    api_key = settings.sources.eodhd.api_key
    if not api_key:
        raise typer.BadParameter(
            "EODHD_API_KEY is not set (add it to .env or your shell environment)"
        )
    return EodhdDataSource(
        api_key=api_key,
        base_url=settings.sources.eodhd.base_url,
        timeout_seconds=settings.sources.eodhd.timeout_seconds,
        max_retries=settings.sources.eodhd.max_retries,
        retry_backoff_seconds=settings.sources.eodhd.retry_backoff_seconds,
    )


def _open_lake(path: Path) -> DuckDBLake:
    return DuckDBLake(path)


def _parse_tickers(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [t.strip() for t in raw.split(",") if t.strip()]


def _validate_tickers_match_asset_class(tickers: list[str], asset_class: AssetClass) -> None:
    """Catch the copy-paste where ``--asset-class crypto --tickers
    BTC-USD.CC,AAPL.US`` would otherwise silently land an equity row
    inside a crypto-flagged ingest run.
    """
    mismatches = [
        (t, classify_asset_class(t)) for t in tickers if classify_asset_class(t) != asset_class
    ]
    if not mismatches:
        return
    detail = ", ".join(f"{t} (classified as {cls})" for t, cls in mismatches)
    raise typer.BadParameter(
        f"--asset-class={asset_class} but the following tickers don't match: {detail}"
    )


# ---- db ---------------------------------------------------------------------


@db_app.command("init")
def db_init() -> None:
    """Create the lake + state files and apply pending migrations to each."""
    settings = _settings()
    with _open_lake(settings.lake.path) as lake:
        lake.migrate()
        lake_versions = lake.applied_migrations()
    with SqliteState(settings.state.path) as state:
        state.migrate()
        state_versions = state.applied_migrations()
    console.print(f"[green]lake ready[/green]  {settings.lake.path} (migrations: {lake_versions})")
    console.print(
        f"[green]state ready[/green] {settings.state.path} (migrations: {state_versions})"
    )


@db_app.command("info")
def db_info() -> None:
    """Print tables and row counts for both the lake and the state DB."""
    settings = _settings()

    lake_table = Table(title=f"lake — {settings.lake.path}")
    lake_table.add_column("table")
    lake_table.add_column("rows", justify="right")
    with _open_lake(settings.lake.path) as lake:
        for name in sorted(lake.tables()):
            lake_table.add_row(name, str(lake.count_rows(name)))
    console.print(lake_table)

    state_table = Table(title=f"state — {settings.state.path}")
    state_table.add_column("table")
    state_table.add_column("rows", justify="right")
    with SqliteState(settings.state.path) as state:
        for name in sorted(state.tables()):
            state_table.add_row(name, str(state.count_rows(name)))
    console.print(state_table)


# ---- ingest -----------------------------------------------------------------


@ingest_app.command("exchanges")
def ingest_exchanges() -> None:
    """List the exchanges supported by the configured data source."""
    import requests

    settings = _settings()
    source = _build_source(settings)
    try:
        rows = sorted(source.list_exchanges(), key=lambda r: r.code)
    except EodhdFreeTierError as exc:
        console.print(f"[yellow]exchanges endpoint blocked on free tier:[/yellow] {exc}")
        raise typer.Exit(code=2) from exc
    except requests.RequestException as exc:
        console.print(f"[red]exchanges fetch failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    table = Table(title=f"exchanges — {source.source_id}")
    table.add_column("code")
    table.add_column("name")
    table.add_column("country")
    table.add_column("currency")
    table.add_column("MIC")
    for row in rows:
        table.add_row(
            row.code,
            row.name or "",
            row.country or "",
            row.currency or "",
            row.operating_mic or "",
        )
    console.print(table)
    console.print(f"[dim]{len(rows)} exchanges[/dim]")


@ingest_app.command("prices")
def ingest_prices(
    tickers: str | None = typer.Option(
        None, "--tickers", help="comma-separated tickers, e.g. AAPL.US,MSFT.US"
    ),
    exchange: str | None = typer.Option(
        None, "--exchange", help="fetch all tickers on this exchange (e.g. US)"
    ),
    asset_class: str | None = typer.Option(
        None,
        "--asset-class",
        help=(
            f"restrict / discover the universe by asset class ({'|'.join(_ASSET_CLASS_CHOICES)}). "
            "For non-equity classes, omitting --tickers and --exchange auto-resolves to EODHD's "
            "virtual exchange (crypto→CC, commodity→COMM, bond→GBOND). "
            "Equity has many real exchanges and requires --exchange or --tickers."
        ),
        callback=_validate_asset_class,
    ),
    since: str | None = typer.Option(
        None, "--since", help="earliest date (YYYY-MM-DD); omit to fetch full history"
    ),
    until: str | None = typer.Option(None, "--until", help="latest date (YYYY-MM-DD)"),
) -> None:
    settings = _settings()
    source = _build_source(settings)

    with _open_lake(settings.lake.path) as lake:
        lake.migrate()
        ticker_list = _parse_tickers(tickers)

        # Asset-class ergonomics: when --asset-class is set, either (a)
        # validate the explicit tickers match the class, or (b) auto-
        # resolve to the virtual exchange for non-equity classes.
        if asset_class is not None:
            ac = cast(AssetClass, asset_class)
            if ticker_list:
                _validate_tickers_match_asset_class(ticker_list, ac)
            elif exchange is None:
                resolved = eodhd_exchange_for_asset_class(ac)
                if resolved is None:
                    raise typer.BadParameter(
                        f"--asset-class={ac} doesn't map to a single exchange — equities "
                        "live on dozens of real exchanges (US/LSE/XETRA/…). "
                        "Pass --exchange or --tickers explicitly."
                    )
                exchange = resolved

        if not ticker_list and exchange:
            ticker_list = source.list_tickers(exchange)
        if not ticker_list:
            raise typer.BadParameter("provide --tickers or --exchange (or --asset-class)")

        since_d = date.fromisoformat(since) if since else None
        until_d = date.fromisoformat(until) if until else None

        pipeline = IngestPipeline(source=source, lake=lake)
        result = pipeline.run_prices(ticker_list, since=since_d, until=until_d)

    _print_result(result)


@ingest_app.command("fundamentals")
def ingest_fundamentals(
    tickers: str = typer.Option(..., "--tickers", help="comma-separated tickers"),
    asset_class: str | None = typer.Option(
        None,
        "--asset-class",
        help=(
            "accepted for symmetry with `ingest prices` / `ingest metadata`, but only "
            f"`equity` is valid: financial statements don't apply to crypto / commodity / bond. "
            f"Allowed: {'|'.join(_ASSET_CLASS_CHOICES)}."
        ),
        callback=_validate_asset_class,
    ),
) -> None:
    """Pull income / balance-sheet / cash-flow statements per ticker.

    Deliberately does not auto-discover a universe via `--asset-class`
    (unlike `ingest prices` / `ingest metadata`): financial statements
    are equity-only by construction, so the natural multi-asset workflow
    here is "name the equity tickers explicitly". Non-equity tickers
    are rejected up-front rather than letting the EODHD adapter silently
    no-op the request.
    """
    settings = _settings()
    source = _build_source(settings)

    if asset_class is not None and asset_class != "equity":
        raise typer.BadParameter(
            f"`ingest fundamentals` is equity-only (income / balance / cashflow "
            f"statements). --asset-class={asset_class} is not supported here; "
            "use `ingest metadata` for crypto / bond / commodity profile data."
        )

    ticker_list = _parse_tickers(tickers)
    non_equity = [t for t in ticker_list if classify_asset_class(t) != "equity"]
    if non_equity:
        joined = ", ".join(non_equity)
        raise typer.BadParameter(
            f"`ingest fundamentals` is equity-only (income / balance / cashflow "
            f"statements). The following tickers are non-equity: {joined}. "
            "Use `ingest metadata` for crypto / bond / commodity profile data."
        )

    with _open_lake(settings.lake.path) as lake:
        lake.migrate()
        pipeline = IngestPipeline(source=source, lake=lake)
        result = pipeline.run_fundamentals(ticker_list)

    _print_result(result)


@ingest_app.command("metadata")
def ingest_metadata(
    tickers: str | None = typer.Option(None, "--tickers", help="comma-separated tickers"),
    asset_class: str | None = typer.Option(
        None,
        "--asset-class",
        help=(
            f"discover the universe by asset class ({'|'.join(_ASSET_CLASS_CHOICES)}); "
            "for non-equity classes, omitting --tickers auto-resolves to EODHD's virtual exchange. "
            "When --tickers is also given, validates each one's classification matches."
        ),
        callback=_validate_asset_class,
    ),
) -> None:
    """Pull the full metadata bundle per ticker.

    For equity tickers, that's profile + dividends + splits + insider
    trades + news + sentiment + analyst estimates + ratings + shares
    outstanding + employee count + market cap history + segmentations.
    For crypto / bond / commodity tickers, the equity-shaped surface is
    skipped and only the asset-class-specific profile (crypto_profile /
    bond_profile / commodity_contract) is populated.
    """
    settings = _settings()
    source = _build_source(settings)

    with _open_lake(settings.lake.path) as lake:
        lake.migrate()
        ticker_list = _parse_tickers(tickers)

        if asset_class is not None:
            ac = cast(AssetClass, asset_class)
            if ticker_list:
                _validate_tickers_match_asset_class(ticker_list, ac)
            else:
                resolved = eodhd_exchange_for_asset_class(ac)
                if resolved is None:
                    raise typer.BadParameter(
                        f"--asset-class={ac} doesn't map to a single exchange — pass "
                        "--tickers explicitly for equity."
                    )
                ticker_list = source.list_tickers(resolved)

        if not ticker_list:
            raise typer.BadParameter("provide --tickers or --asset-class")

        pipeline = IngestPipeline(source=source, lake=lake)
        result = pipeline.run_metadata(ticker_list)

    _print_result(result)


@ingest_app.command("intraday")
def ingest_intraday(
    tickers: str = typer.Option(..., "--tickers", help="comma-separated tickers"),
    interval: str = typer.Option("5m", "--interval", help="native intraday: 1m | 5m | 1h"),
    since: str | None = typer.Option(None, "--since", help="earliest date (YYYY-MM-DD)"),
    until: str | None = typer.Option(None, "--until", help="latest date (YYYY-MM-DD)"),
) -> None:
    """Pull sub-daily OHLCV bars at a native intraday interval. Only 1m,
    5m, and 1h are available from EODHD; coarser sub-daily bars (4h, 6h,
    12h) are produced by ``stonks ingest aggregate``."""
    from stonks.core.interval import Interval

    settings = _settings()
    source = _build_source(settings)
    parsed = Interval.parse(interval)

    with _open_lake(settings.lake.path) as lake:
        lake.migrate()
        ticker_list = _parse_tickers(tickers)
        since_d = date.fromisoformat(since) if since else None
        until_d = date.fromisoformat(until) if until else None
        pipeline = IngestPipeline(source=source, lake=lake)
        result = pipeline.run_intraday_bars(ticker_list, parsed, since=since_d, until=until_d)

    _print_result(result)


@ingest_app.command("aggregate")
def ingest_aggregate(
    tickers: str = typer.Option(..., "--tickers", help="comma-separated tickers"),
    source_interval: str = typer.Option(..., "--from", help="source interval (e.g. 1h, 1d, 1mo)"),
    target_interval: str = typer.Option(
        ..., "--to", help="target interval (must be coarser, e.g. 4h, 3d, 6mo)"
    ),
) -> None:
    """Derive coarser-interval bars by time-bucketing stored source bars.
    Source bars must already be ingested (via ``ingest prices`` or
    ``ingest intraday``)."""
    from stonks.core.interval import Interval

    settings = _settings()
    src_iv = Interval.parse(source_interval)
    tgt_iv = Interval.parse(target_interval)

    with _open_lake(settings.lake.path) as lake:
        lake.migrate()
        ticker_list = _parse_tickers(tickers)
        for ticker in ticker_list:
            delta = lake.aggregate_bars(ticker, src_iv, tgt_iv)
            console.print(f"[green]{ticker}[/green] {src_iv.code} → {tgt_iv.code}  +{delta} rows")


@ingest_app.command("all-intervals")
def ingest_all_intervals(
    tickers: str = typer.Option(..., "--tickers", help="comma-separated tickers"),
    since: str | None = typer.Option(
        None, "--since", help="earliest date (YYYY-MM-DD); applies to daily + intraday fetches"
    ),
    until: str | None = typer.Option(None, "--until", help="latest date (YYYY-MM-DD)"),
    intraday_since: str | None = typer.Option(
        None,
        "--intraday-since",
        help="separate start date for intraday pulls (1m/5m/1h); defaults to --since if omitted, "
        "but EODHD caps 1m history at ~120 days so setting this explicitly avoids long failing fetches",
    ),
) -> None:
    """Populate every canonical interval for each ticker: native 1m / 5m /
    1h / 1d / 1w / 1mo from EODHD, plus derived 4h / 6h / 12h / 3d / 5d /
    6mo / 1y / 5y via aggregation."""
    from stonks.core.interval import Interval

    settings = _settings()
    source = _build_source(settings)

    since_d = date.fromisoformat(since) if since else None
    until_d = date.fromisoformat(until) if until else None
    intraday_since_d = date.fromisoformat(intraday_since) if intraday_since else since_d

    with _open_lake(settings.lake.path) as lake:
        lake.migrate()
        ticker_list = _parse_tickers(tickers)
        pipeline = IngestPipeline(source=source, lake=lake)

        # 1. daily — uses the existing EOD endpoint; full history by default.
        console.print("[bold]→ fetching 1d[/bold]")
        _print_result(pipeline.run_prices(ticker_list, since=since_d, until=until_d))

        # 2. native intraday
        for iv in (Interval.HOUR_1, Interval.MIN_5, Interval.MIN_1):
            console.print(f"[bold]→ fetching {iv.code}[/bold]")
            _print_result(
                pipeline.run_intraday_bars(ticker_list, iv, since=intraday_since_d, until=until_d)
            )

        # 3. derived — aggregate from the finest source that's both stored
        #    and strictly finer than the target.
        derivations: list[tuple[Interval, Interval]] = [
            (Interval.HOUR_1, Interval.HOUR_4),
            (Interval.HOUR_1, Interval.HOUR_6),
            (Interval.HOUR_1, Interval.HOUR_12),
            (Interval.DAY_1, Interval.DAY_3),
            (Interval.DAY_1, Interval.DAY_5),
            (Interval.DAY_1, Interval.WEEK_1),
            (Interval.DAY_1, Interval.MONTH_1),
            (Interval.DAY_1, Interval.MONTH_6),
            (Interval.DAY_1, Interval.YEAR_1),
            (Interval.DAY_1, Interval.YEAR_5),
        ]
        for src_iv, tgt_iv in derivations:
            console.print(f"[dim]aggregating {src_iv.code} → {tgt_iv.code}[/dim]")
            for ticker in ticker_list:
                try:
                    lake.aggregate_bars(ticker, src_iv, tgt_iv)
                except Exception as exc:
                    console.print(
                        f"[red]aggregate {src_iv.code}→{tgt_iv.code} for {ticker} failed:[/red] {exc}"
                    )


def _print_result(result: IngestRunResult) -> None:
    color = {"ok": "green", "partial": "yellow", "error": "red"}.get(result.status, "white")
    console.print(
        f"[{color}]run #{result.run_id} — kind={result.kind} status={result.status} "
        f"ok={result.tickers_ok} failed={result.tickers_failed}[/{color}]"
    )


def _open_registry(settings: Settings) -> tuple[SqliteState, StrategyRegistry]:
    state = SqliteState(settings.state.path)
    return state, StrategyRegistry(state=state, artifacts_dir=settings.registry.artifacts_dir)


@registry_app.command("list")
def registry_list(
    status: str | None = typer.Option(
        None, "--status", help="filter by status (active|shadow|retired)"
    ),
    asset_class: str | None = typer.Option(
        None,
        "--asset-class",
        help="filter to strategies whose applicable_asset_classes include this class "
        f"({'|'.join(_ASSET_CLASS_CHOICES)})",
        callback=_validate_asset_class,
    ),
) -> None:
    settings = _settings()
    state, registry = _open_registry(settings)
    try:
        handles = registry.list_all(status=status)
        if asset_class is not None:
            # Read the class attribute via importlib so we don't run any
            # strategy constructor just to introspect a ClassVar.
            handles = [
                h for h in handles if asset_class in _strategy_applicable_classes(h.class_path)
            ]
        title = f"strategies ({status or 'all'}"
        if asset_class is not None:
            title += f", asset_class={asset_class}"
        title += ")"
        table = Table(title=title)
        table.add_column("id", no_wrap=True, overflow="fold")
        for col in ("status", "class_path", "params", "created_at"):
            table.add_column(col)
        for h in handles:
            table.add_row(
                h.id,
                h.status,
                h.class_path,
                ",".join(f"{k}={v}" for k, v in sorted(h.params.items())),
                h.created_at,
            )
        console.print(table)
    finally:
        state.close()


@registry_app.command("show")
def registry_show(strategy_id: str) -> None:
    settings = _settings()
    state, registry = _open_registry(settings)
    try:
        handles = registry.list_all()
        match = next((h for h in handles if h.id == strategy_id), None)
        if match is None:
            console.print(f"[red]no strategy with id {strategy_id!r}[/red]")
            raise typer.Exit(code=1)
        console.print(f"[bold]{match.id}[/bold]  status=[cyan]{match.status}[/cyan]")
        console.print(f"class_path: {match.class_path}")
        console.print(f"artifact_path: {match.artifact_path}")
        console.print(f"params: {match.params}")
        reports = registry.get_reports(strategy_id)
        rtable = Table(title="survival reports")
        for col in ("test_id", "passed", "metrics"):
            rtable.add_column(col)
        for r in reports:
            rtable.add_row(r.test_id, "yes" if r.passed else "no", str(dict(r.metrics)))
        console.print(rtable)
    finally:
        state.close()


def _set_status_or_exit(registry: StrategyRegistry, strategy_id: str, status: str) -> None:
    try:
        registry.set_status(strategy_id, status)
    except KeyError:
        console.print(f"[red]no strategy with id {strategy_id!r}[/red]")
        raise typer.Exit(code=1) from None


@registry_app.command("promote")
def registry_promote(strategy_id: str) -> None:
    settings = _settings()
    state, registry = _open_registry(settings)
    try:
        _set_status_or_exit(registry, strategy_id, "active")
        console.print(f"[green]{strategy_id} → active[/green]")
    finally:
        state.close()


@registry_app.command("retire")
def registry_retire(strategy_id: str) -> None:
    settings = _settings()
    state, registry = _open_registry(settings)
    try:
        _set_status_or_exit(registry, strategy_id, "retired")
        console.print(f"[yellow]{strategy_id} → retired[/yellow]")
    finally:
        state.close()


# ---- production tick --------------------------------------------------------


@app.command("tick")
def tick(
    dry_run: bool = typer.Option(False, "--dry-run", help="rank + log, place no orders"),
    as_of: str | None = typer.Option(
        None, "--as-of", help="override date (YYYY-MM-DD); default is today"
    ),
    tickers: str | None = typer.Option(
        None,
        "--tickers",
        help="comma-separated universe; overrides config.production.universe",
    ),
    asset_class: str | None = typer.Option(
        None,
        "--asset-class",
        help="restrict universe to instruments of this class "
        f"({'|'.join(_ASSET_CLASS_CHOICES)}); requires the instruments table "
        "to have asset_class populated for the relevant tickers",
        callback=_validate_asset_class,
    ),
) -> None:
    """One-shot production tick. Rank active strategies × universe, pick a
    winner, let it decide, execute idempotently through the broker, and
    record everything in state. Designed to be invoked by cron/systemd."""
    settings = _settings()

    universe = _parse_tickers(tickers) or list(settings.production.universe)
    if not universe:
        raise typer.BadParameter(
            "production universe is empty — provide --tickers or set "
            "[production].universe in config/default.toml"
        )

    as_of_date = date.fromisoformat(as_of) if as_of else date.today()
    state, registry = _open_registry(settings)
    try:
        with _open_lake(settings.lake.path) as lake:
            # Apply --asset-class inside the same lake connection that
            # ``run_tick`` will use, so we don't open the lake twice.
            if asset_class is not None:
                classes = lake.get_asset_classes(universe)
                universe = [t for t in universe if classes.get(t) == asset_class]
                if not universe:
                    raise typer.BadParameter(
                        f"no instruments in the universe match --asset-class={asset_class!r}; "
                        "ingest profiles first or relax the filter"
                    )

            tick_settings = TickSettings(
                universe=universe,
                threshold=settings.production.threshold,
                initial_cash=settings.production.initial_cash,
                slippage_bps=settings.production.slippage_bps,
                fee_per_trade=settings.production.fee_per_trade,
            )

            result = run_tick(
                state=state,
                lake=lake,
                registry=registry,
                settings=tick_settings,
                as_of=as_of_date,
                dry_run=dry_run,
            )
    finally:
        state.close()

    color = {"ok": "green", "partial": "yellow", "error": "red", "noop": "cyan"}.get(
        result.status, "white"
    )
    console.print(
        f"[{color}]{result.tick_id}[/{color}]  status={result.status}  "
        f"winner={result.winner_strategy_id or '-'}  "
        f"orders={result.orders_placed}  fills={result.fills}"
        + ("  [dim](dry-run)[/dim]" if dry_run else "")
    )


# Re-export bound logger so tests / users can discover it easily
log = get_logger("stonks.cli")


if __name__ == "__main__":
    app()

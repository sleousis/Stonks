"""Typer CLI entrypoint — ``stonks`` command. Run ``stonks --help`` for the subcommand surface.

To browse the lake interactively, run ``uv run duckdb -ui data/lake.duckdb``
(requires the standalone DuckDB CLI on PATH: https://install.duckdb.org).
"""

from __future__ import annotations

import importlib
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, cast, get_args

import typer
from rich.console import Console
from rich.table import Table

from stonks.config import Settings, load_settings
from stonks.core.types import AssetClass
from stonks.ingest.pipeline import IngestRunResult
from stonks.ingest.sources.base import DataSource, DataSourceError
from stonks.ingest.sources.eodhd import (
    EODHD_DEFAULT_MACRO_INDICATOR,
    EODHD_MACRO_INDICATORS,
    EodhdFreeTierError,
    classify_asset_class,
    eodhd_exchange_for_asset_class,
)
from stonks.ingest.sources.registry import (
    DEFAULT_SOURCE_ID,
    SOURCE_IDS,
    SourceConfigError,
    build_source,
)
from stonks.ingest.wiring import build_ingest_pipeline
from stonks.logging import configure_logging, get_logger
from stonks.notify import notifier_from_settings
from stonks.ops.commands import app as ops_app
from stonks.production.settings_builder import build_tick_runtime
from stonks.production.tick import BackdatedTickError, run_tick
from stonks.registry.store import StrategyRegistry
from stonks.store.lake import DuckDBLake
from stonks.store.state import SqliteState
from stonks.universes.commands import app as universe_app

_ASSET_CLASS_CHOICES: tuple[str, ...] = get_args(AssetClass)


def _validate_asset_class(value: str | None) -> str | None:
    if value is None or value in _ASSET_CLASS_CHOICES:
        return value
    raise typer.BadParameter(
        f"--asset-class must be one of {list(_ASSET_CLASS_CHOICES)}, got {value!r}"
    )


def _validate_iso_date(
    ctx: typer.Context, param: typer.CallbackParam, value: str | None
) -> str | None:
    """Reject malformed date options at parse time with a usage error
    instead of letting ``date.fromisoformat`` raise a traceback later."""
    if value is None:
        return value
    try:
        date.fromisoformat(value)
    except ValueError:
        raise typer.BadParameter(
            f"expected a date in YYYY-MM-DD format, got {value!r}", ctx=ctx, param=param
        ) from None
    return value


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
app.add_typer(ops_app, name="backup")
app.add_typer(universe_app, name="universe")


@app.command(
    "schedule",
    context_settings={
        "allow_extra_args": True,
        "ignore_unknown_options": True,
        "help_option_names": [],
    },
    add_help_option=False,
)
def schedule(ctx: typer.Context) -> None:
    """Built-in scheduler: run | next | runs | run-now JOB | check | metrics
    (``stonks schedule --help`` for options; ``\\[scheduler]`` in the config)."""
    from stonks.scheduling.__main__ import main as schedule_main

    raise typer.Exit(code=schedule_main(list(ctx.args), prog="stonks schedule"))


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


def _validate_source(value: str) -> str:
    if value in SOURCE_IDS:
        return value
    raise typer.BadParameter(f"--source must be one of {list(SOURCE_IDS)}, got {value!r}")


def _source_option() -> typer.models.OptionInfo:
    # A fresh OptionInfo per command: Typer mutates them during registration.
    return typer.Option(
        DEFAULT_SOURCE_ID,
        "--source",
        help=f"data source ({'|'.join(SOURCE_IDS)})",
        callback=_validate_source,
    )


def _build_source(settings: Settings, source_id: str) -> DataSource:
    try:
        return build_source(source_id, settings.sources)
    except SourceConfigError as exc:
        raise typer.BadParameter(str(exc)) from exc


def _list_tickers_or_usage_error(source: DataSource, exchange: str) -> list[str]:
    try:
        return source.list_tickers(exchange)
    except DataSourceError as exc:
        raise typer.BadParameter(
            f"source {source.source_id!r} cannot list tickers for {exchange!r} ({exc}); "
            "pass --tickers explicitly"
        ) from exc


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
def ingest_exchanges(source_id: str = _source_option()) -> None:
    """List the exchanges supported by the configured data source."""
    import requests

    settings = _settings()
    source = _build_source(settings, source_id)
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
        None,
        "--since",
        help="earliest date (YYYY-MM-DD); omit to fetch full history",
        callback=_validate_iso_date,
    ),
    until: str | None = typer.Option(
        None, "--until", help="latest date (YYYY-MM-DD)", callback=_validate_iso_date
    ),
    source_id: str = _source_option(),
) -> None:
    settings = _settings()
    source = _build_source(settings, source_id)

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
            ticker_list = _list_tickers_or_usage_error(source, exchange)
        if not ticker_list:
            raise typer.BadParameter("provide --tickers or --exchange (or --asset-class)")

        since_d = date.fromisoformat(since) if since else None
        until_d = date.fromisoformat(until) if until else None

        pipeline = build_ingest_pipeline(settings, source, lake)
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
    source_id: str = _source_option(),
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
    source = _build_source(settings, source_id)

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
        pipeline = build_ingest_pipeline(settings, source, lake)
        result = pipeline.run_fundamentals(ticker_list)
        # BL-36: re-check the accounting identities of what just landed.
        report = _audit(settings, lake, ticker_list)

    _print_result(result)
    _print_audit(report)


def _audit(settings: Settings, lake: DuckDBLake, tickers: list[str] | None) -> Any:
    """Run the statement audit at the ``[audit]`` tolerances."""
    from stonks.store.audit import audit_statements, build_checks

    return audit_statements(lake, tickers, checks=build_checks(settings.audit.tolerances()))


def _print_audit(report: Any) -> None:
    if not report.counts:
        console.print("statement audit: [green]no flags[/green]")
        return
    table = Table(title=f"statement audit: {report.n_flags} flag(s)")
    for col in ("check", "flags"):
        table.add_column(col)
    for check_id, n in sorted(report.counts.items()):
        table.add_row(check_id, str(n))
    console.print(table)


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
    source_id: str = _source_option(),
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
    source = _build_source(settings, source_id)

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
                ticker_list = _list_tickers_or_usage_error(source, resolved)

        if not ticker_list:
            raise typer.BadParameter("provide --tickers or --asset-class")

        pipeline = build_ingest_pipeline(settings, source, lake)
        result = pipeline.run_metadata(ticker_list)

    _print_result(result)


@ingest_app.command("intraday")
def ingest_intraday(
    tickers: str = typer.Option(..., "--tickers", help="comma-separated tickers"),
    interval: str = typer.Option(
        "5m", "--interval", help="native intraday: eodhd 1m|5m|1h, yahoo 1m|5m|15m|30m|1h"
    ),
    since: str | None = typer.Option(
        None, "--since", help="earliest date (YYYY-MM-DD)", callback=_validate_iso_date
    ),
    until: str | None = typer.Option(
        None, "--until", help="latest date (YYYY-MM-DD)", callback=_validate_iso_date
    ),
    source_id: str = _source_option(),
) -> None:
    """Pull sub-daily OHLCV bars at a native intraday interval (EODHD:
    1m, 5m, 1h; Yahoo: 1m, 5m, 15m, 30m, 1h within Yahoo's lookback
    limits). Coarser sub-daily bars (4h, 6h, 12h) are produced by
    ``stonks ingest aggregate``."""
    from stonks.core.interval import Interval

    settings = _settings()
    source = _build_source(settings, source_id)
    parsed = Interval.parse(interval)

    with _open_lake(settings.lake.path) as lake:
        lake.migrate()
        ticker_list = _parse_tickers(tickers)
        since_d = date.fromisoformat(since) if since else None
        until_d = date.fromisoformat(until) if until else None
        pipeline = build_ingest_pipeline(settings, source, lake)
        result = pipeline.run_intraday_bars(ticker_list, parsed, since=since_d, until=until_d)

    _print_result(result)


@ingest_app.command("macro")
def ingest_macro(
    countries: str = typer.Option(
        ...,
        "--countries",
        help="comma-separated ISO 3166-1 alpha-3 country codes, e.g. USA,DEU,GBR",
    ),
    indicators: str | None = typer.Option(
        None,
        "--indicators",
        help=(
            "comma-separated macro indicator keys (snake_case). "
            "Omit to fall back to EODHD's default ('gdp_current_usd'); "
            "pass 'all' to ingest the full vendor catalog. "
            "Reference list: https://eodhd.com/financial-apis/macroeconomics-data-api/"
        ),
    ),
    source_id: str = _source_option(),
) -> None:
    """Pull macroeconomic time series for one or more countries and indicators.

    Each (country, indicator) pair is one EODHD call; rows are upserted into
    the ``macro_indicators`` lake table keyed by
    ``(country_iso, indicator, observation_date)``. Re-running is idempotent;
    vendor revisions to a previously-published value land in place.
    """
    settings = _settings()
    source = _build_source(settings, source_id)

    country_list = _parse_tickers(countries)
    if not country_list:
        raise typer.BadParameter("--countries requires at least one ISO-3 code")
    # ``isalpha()`` returns True for non-ASCII letters (Greek, Cyrillic,
    # accented Latin); the explicit ``isascii()`` keeps the check aligned
    # with the "three ASCII letters" promise we make to operators.
    bad_countries = [c for c in country_list if len(c) != 3 or not (c.isascii() and c.isalpha())]
    if bad_countries:
        raise typer.BadParameter(
            f"--countries entries must be ISO 3166-1 alpha-3 (e.g. USA); invalid: {bad_countries}"
        )
    country_list = [c.upper() for c in country_list]

    if indicators is None:
        indicator_list: list[str] = [EODHD_DEFAULT_MACRO_INDICATOR]
    elif indicators.strip().lower() == "all":
        indicator_list = list(EODHD_MACRO_INDICATORS)
    else:
        indicator_list = _parse_tickers(indicators)
        if not indicator_list:
            raise typer.BadParameter("--indicators must be non-empty (or omitted, or 'all')")

    with _open_lake(settings.lake.path) as lake:
        lake.migrate()
        pipeline = build_ingest_pipeline(settings, source, lake)
        result = pipeline.run_macro_indicators(
            countries=country_list,
            indicators=indicator_list,
        )

    _print_result(result)


@ingest_app.command("tvl")
def ingest_tvl(
    chains: str = typer.Option(
        ...,
        "--chains",
        help="comma-separated chain names, e.g. ethereum,solana (case-insensitive)",
    ),
    since: str | None = typer.Option(
        None,
        "--since",
        help="earliest observation date (YYYY-MM-DD); omit for the full history",
        callback=_validate_iso_date,
    ),
    source_id: str = typer.Option(
        "defillama",
        "--source",
        help=f"data source ({'|'.join(SOURCE_IDS)}); TVL is served by defillama",
        callback=_validate_source,
    ),
) -> None:
    """Pull daily DeFi total value locked per chain into ``defi_tvl``.

    Each chain is one unit in the ``ingest_runs`` row (an unknown chain
    soft-fails without blocking the rest). Re-running is idempotent.
    """
    from stonks.ingest.sources.defillama import normalize_chain

    chain_list = [normalize_chain(c) for c in _parse_tickers(chains)]
    if not chain_list:
        raise typer.BadParameter("--chains requires at least one chain name")
    settings = _settings()
    source = _build_source(settings, source_id)
    since_d = date.fromisoformat(since) if since else None

    with _open_lake(settings.lake.path) as lake:
        lake.migrate()
        pipeline = build_ingest_pipeline(settings, source, lake)
        result = pipeline.run_defi_tvl(chain_list, since=since_d)

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
        None,
        "--since",
        help="earliest date (YYYY-MM-DD); applies to daily + intraday fetches",
        callback=_validate_iso_date,
    ),
    until: str | None = typer.Option(
        None, "--until", help="latest date (YYYY-MM-DD)", callback=_validate_iso_date
    ),
    intraday_since: str | None = typer.Option(
        None,
        "--intraday-since",
        help="separate start date for intraday pulls (1m/5m/1h); defaults to --since if omitted, "
        "but EODHD caps 1m history at ~120 days so setting this explicitly avoids long failing fetches",
        callback=_validate_iso_date,
    ),
    source_id: str = _source_option(),
) -> None:
    """Populate every canonical interval for each ticker: native 1m / 5m /
    1h / 1d from the source, plus derived 4h / 6h / 12h / 3d / 5d / 1w /
    1mo / 6mo / 1y / 5y via aggregation."""
    from stonks.core.interval import Interval

    settings = _settings()
    source = _build_source(settings, source_id)

    since_d = date.fromisoformat(since) if since else None
    until_d = date.fromisoformat(until) if until else None
    intraday_since_d = date.fromisoformat(intraday_since) if intraday_since else since_d

    with _open_lake(settings.lake.path) as lake:
        lake.migrate()
        ticker_list = _parse_tickers(tickers)
        pipeline = build_ingest_pipeline(settings, source, lake)

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
    """List registered strategies with their status."""
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
    """Show one strategy: status, class, artifact folder and params."""
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


def _cli_actor() -> str:
    """``cli:<os user>``: who a CLI status change is logged under."""
    import getpass

    try:
        return f"cli:{getpass.getuser()}"
    except Exception:  # no login name (some containers / services)
        return "cli"


def _reason_option() -> typer.models.OptionInfo:
    return typer.Option(None, "--reason", help="why (logged; required for demotions and overrides)")


def _change_status_or_exit(
    settings: Settings,
    strategy_id: str,
    status: str,
    *,
    reason: str | None,
    override: bool = False,
) -> None:
    """Change a strategy's status through the governed service
    (``app.strategies.change_status``: go-live gate for promotions, audit
    row for every change). Refusals print a friendly message and exit 1."""
    from stonks.app.context import AppContext
    from stonks.app.errors import AppError, NotFoundError
    from stonks.app.strategies import PromotionRefusedError, change_status

    with SqliteState(settings.state.path) as state:
        state.migrate()
    try:
        change = change_status(
            AppContext(settings),
            strategy_id,
            status,
            actor=_cli_actor(),
            reason=reason,
            override=override,
        )
    except NotFoundError:
        console.print(f"[red]no strategy with id {strategy_id!r}[/red]")
        raise typer.Exit(code=1) from None
    except PromotionRefusedError as exc:
        console.print(f"[red]promotion refused: {exc}[/red]")
        if exc.failures:
            console.print("failing go-live checks:")
            for line in exc.failures:
                console.print(f"  - {line}")
        console.print(
            "See `stonks golive check <id>`, or pass --override with a --reason "
            "of at least 20 characters."
        )
        raise typer.Exit(code=1) from None
    except AppError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None
    colour = {"active": "green", "shadow": "cyan", "retired": "yellow"}[status]
    if change is None:
        console.print(f"[{colour}]{strategy_id} is already {status}[/{colour}]")
    else:
        console.print(f"[{colour}]{strategy_id} → {status}[/{colour}]")


@registry_app.command("promote")
def registry_promote(
    strategy_id: str,
    reason: str | None = _reason_option(),
    override: bool = typer.Option(
        False,
        "--override",
        help="promote without a passing go-live check (needs --reason, >= 20 chars)",
    ),
) -> None:
    """Move a strategy to active. Needs a passing go-live check, or
    --override with a reason; every change is audited."""
    _change_status_or_exit(_settings(), strategy_id, "active", reason=reason, override=override)


@registry_app.command("shadow")
def registry_shadow(strategy_id: str, reason: str | None = _reason_option()) -> None:
    """Move a strategy back to shadow (paper-traded on a virtual portfolio)."""
    _change_status_or_exit(_settings(), strategy_id, "shadow", reason=reason)


@registry_app.command("retire")
def registry_retire(strategy_id: str, reason: str | None = _reason_option()) -> None:
    """Retire a strategy: it is no longer ranked or evaluated."""
    _change_status_or_exit(_settings(), strategy_id, "retired", reason=reason)


@registry_app.command("history")
def registry_history(strategy_id: str) -> None:
    """The strategy's audited status changes and interventions, oldest first."""
    settings = _settings()
    state, registry = _open_registry(settings)
    try:
        state.migrate()
        if not any(h.id == strategy_id for h in registry.list_all()):
            console.print(f"[red]no strategy with id {strategy_id!r}[/red]")
            raise typer.Exit(code=1)
        table = Table(title=f"status history: {strategy_id}")
        for col in ("when", "kind", "change", "actor", "override", "go-live", "reason"):
            table.add_column(col)
        for c in registry.status_history(strategy_id):
            golive = "-" if c.golive_passed is None else ("pass" if c.golive_passed else "fail")
            table.add_row(
                c.created_at,
                c.kind,
                f"{c.from_status} → {c.to_status}" if c.to_status else "-",
                c.actor,
                "override" if c.override else "",
                golive,
                c.reason,
            )
        console.print(table)
    finally:
        state.close()


# ---- production tick --------------------------------------------------------


def _production_universe(
    lake: Any,
    settings: Any,
    as_of: date | None,
    tickers: str | None,
    *,
    required: bool = True,
) -> list[str]:
    """``--tickers``, else ``[production].universe`` (a list, or a universe
    id resolved on ``as_of``, default today in UTC). ``required=False``
    gives ``[]`` instead of an error."""
    from stonks.production.universe import EmptyUniverseError, production_tickers

    day = as_of or datetime.now(UTC).date()
    try:
        return production_tickers(
            lake, settings.production.universe, day, tickers=_parse_tickers(tickers)
        )
    except EmptyUniverseError as exc:
        if not required:
            return []
        raise typer.BadParameter(str(exc), param_hint="--tickers") from None


@app.command("tick")
def tick(
    dry_run: bool = typer.Option(False, "--dry-run", help="rank + log, place no orders"),
    as_of: str | None = typer.Option(
        None,
        "--as-of",
        help="override date (YYYY-MM-DD); default is today in UTC",
        callback=_validate_iso_date,
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
    """One-shot production tick. Score every active strategy on the
    universe, build each portfolio's orders with its constructor (the
    default single_winner lets the top strategy decide), execute them
    idempotently through the broker, and record everything in state.
    Designed to be invoked by cron or systemd."""
    settings = _settings()

    configured = settings.production.universe
    if not _parse_tickers(tickers) and not configured:
        raise typer.BadParameter(
            "production universe is empty — provide --tickers or set "
            "[production].universe in config/default.toml"
        )

    # None → run_tick defaults to the UTC date (stored timestamps are UTC).
    as_of_date = date.fromisoformat(as_of) if as_of else None
    state, registry = _open_registry(settings)
    try:
        with _open_lake(settings.lake.path) as lake:
            universe = _production_universe(lake, settings, as_of_date, tickers)
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

            # explicit tickers narrow the tick: other holdings are left alone
            scoped = bool(_parse_tickers(tickers)) or asset_class is not None
            runtime = build_tick_runtime(settings, universe, scoped=scoped)
            try:
                result = run_tick(
                    state=state,
                    lake=lake,
                    registry=registry,
                    settings=runtime.settings,
                    as_of=as_of_date,
                    dry_run=dry_run,
                    notifier=runtime.notifier,
                    broker_factory=runtime.broker_factory,
                    plan=runtime.plan_for(state),
                )
            except BackdatedTickError as exc:
                console.print(f"[red]{exc}[/red]")
                raise typer.Exit(code=1) from None
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


# ---- operations: health + pnl ----------------------------------------------


@app.command("health")
def health(
    tickers: str | None = typer.Option(
        None,
        "--tickers",
        help="comma-separated tickers to check for fresh bars; overrides config.production.universe",
    ),
    notify: bool = typer.Option(
        False, "--notify/--no-notify", help="send an alert through \\[notify] when unhealthy"
    ),
) -> None:
    """Check data freshness, stuck/failed runs and open risk halts. Stale data
    or a stuck run opens the global operational halt (cleared by the next
    healthy check). Exit code 1 when unhealthy, so a scheduler can alert."""
    from stonks.production.halts import run_health
    from stonks.production.health import notify_unhealthy

    settings = _settings()
    state = SqliteState(settings.state.path)
    try:
        with _open_lake(settings.lake.path) as lake:
            universe = _production_universe(lake, settings, None, tickers, required=False)
            report = run_health(state, lake, universe, settings.production.health)
    finally:
        state.close()

    table = Table(title="health")
    for col in ("check", "status", "detail"):
        table.add_column(col)
    for c in report.checks:
        table.add_row(c.name, "[green]ok[/green]" if c.ok else "[red]FAIL[/red]", c.detail)
    console.print(table)

    if report.healthy:
        console.print("[green]healthy[/green]")
        return
    console.print(f"[red]UNHEALTHY[/red]: {len(report.failures)} check(s) failed")
    if notify:
        notify_unhealthy(report, notifier_from_settings(settings))
    raise typer.Exit(code=1)


@app.command("pnl")
def pnl(
    since: str | None = typer.Option(
        None,
        "--since",
        help="first day to show (YYYY-MM-DD); returns are still measured from inception",
        callback=_validate_iso_date,
    ),
    strategy: str | None = typer.Option(
        None, "--strategy", help="show a shadow strategy's virtual P&L instead of the real one"
    ),
) -> None:
    """Daily P&L from portfolio snapshots, one row per tick as_of: value,
    change since the previous row (blank when more than 4 days apart, see
    ``days``), cumulative return and drawdown from the running peak."""
    from stonks.production.pnl import load_pnl

    settings = _settings()
    since_d = date.fromisoformat(since) if since else None
    state = SqliteState(settings.state.path)
    try:
        rows = load_pnl(state, since=since_d, strategy_id=strategy)
    finally:
        state.close()

    if not rows:
        console.print("[yellow]no portfolio snapshots yet[/yellow]")
        return

    def pct(x: float | None) -> str:
        return "-" if x is None else f"{x:+.2%}"

    title = f"P&L ({'shadow ' + strategy if strategy else 'portfolio'})"
    table = Table(title=title)
    for col in ("date", "days", "value", "change", "daily", "cumulative", "drawdown"):
        table.add_column(col, justify="right")
    for r in rows:
        table.add_row(
            r.day.isoformat(),
            "-" if r.days_elapsed is None else str(r.days_elapsed),
            f"{r.total_value:,.2f}",
            "-" if r.daily_change is None else f"{r.daily_change:+,.2f}",
            pct(r.daily_return),
            pct(r.cumulative_return),
            pct(r.drawdown),
        )
    console.print(table)


# Re-export bound logger so tests / users can discover it easily
log = get_logger("stonks.cli")


# ---- REST API server --------------------------------------------------------


@app.command("serve")
def serve(
    host: str | None = typer.Option(None, "--host", help="bind address; default \\[api].host"),
    port: int | None = typer.Option(None, "--port", help="bind port; default \\[api].port"),
    reload: bool = typer.Option(False, "--reload", help="auto-reload on code changes (dev)"),
) -> None:
    """Run the REST API (and the built UI from web/dist, if present) with uvicorn.

    Binds 127.0.0.1 by default. Every call needs a sign-in or an API token
    (docs/security.md). X-Forwarded-For is trusted only from
    \\[api].trusted_proxies (env STONKS_API_TRUSTED_PROXIES).
    """
    import uvicorn

    settings = _settings()
    bind_host = host or settings.api.host
    bind_port = port or settings.api.port
    if settings.api.open_reads_on_loopback:
        console.print("[yellow]dev profile: reads from 127.0.0.1 need no credential[/yellow]")
    if bind_host not in ("127.0.0.1", "localhost", "::1"):
        log.warning("serve.non_loopback_bind", host=bind_host)
    uvicorn.run(
        "stonks.api.server:app_factory",
        factory=True,
        host=bind_host,
        port=bind_port,
        reload=reload,
        log_config=None,
        # The client IP feeds the login limit and the audit log: believe a
        # forwarded header only from the reverse proxy.
        proxy_headers=True,
        forwarded_allow_ips=",".join(settings.api.trusted_proxies),
    )


# ---- users ------------------------------------------------------------------

users_app = typer.Typer(
    help="People who can sign in. Shell access to the server implies admin. "
    "Passwords come from STONKS_AUTH_PASSWORD or a no-echo prompt, never an option."
)
app.add_typer(users_app, name="users")


def _auth_service(settings: Settings) -> Any:
    from contextlib import contextmanager

    from stonks.auth.service import AuthService

    @contextmanager
    def state_factory():
        with SqliteState(settings.state.path) as state:
            yield state

    return AuthService(state_factory, settings=settings.auth)


def _users_call(fn: Any) -> Any:
    """Run ``fn`` and turn an app error into a red line and exit code 1."""
    from stonks.app.errors import AppError

    try:
        return fn()
    except AppError as exc:
        console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(1) from None


@users_app.command("bootstrap")
def users_bootstrap(
    email: str = typer.Option(..., "--email", help="the first admin's sign-in email"),
) -> None:
    """Give the bootstrap admin an email and a password so the first sign-in
    works. The second factor is set up at that sign-in."""
    from stonks.auth.prompt import read_new_password

    svc = _auth_service(_settings())
    user = _users_call(lambda: svc.bootstrap_admin(email, read_new_password()))
    console.print(f"bootstrap admin {user.id} can now sign in as {user.email}")


@users_app.command("reset-password")
def users_reset_password(
    email: str = typer.Option(..., "--email", help="the person's sign-in email"),
) -> None:
    """Set a new password for a person and sign them out everywhere."""
    from stonks.auth.prompt import read_new_password

    svc = _auth_service(_settings())
    user = _users_call(lambda: svc.set_password_by_email(email, read_new_password()))
    console.print(f"password reset for {user.id}; their sessions were signed out")


@users_app.command("list")
def users_list() -> None:
    """Everyone with an account: email, role, status, second factor. No holdings."""
    from stonks.accounts import DEFAULT_OWNER_ID, Role
    from stonks.auth import Principal
    from stonks.auth.principal import ROLE_SCOPES

    svc = _auth_service(_settings())
    cli = Principal.create(
        user_id=DEFAULT_OWNER_ID,
        kind="human",
        role=Role.ADMIN,
        scopes=ROLE_SCOPES[Role.ADMIN],
        mfa_fresh=False,
        via="cli",
    )
    people = _users_call(lambda: svc.list_users(cli))
    table = Table(title="Users")
    for col in ("id", "email", "name", "role", "status", "2FA"):
        table.add_column(col)
    for info in people:
        u = info.user
        table.add_row(
            u.id,
            u.email or "-",
            u.display_name,
            u.role.value,
            u.status,
            "yes" if info.mfa_enrolled else "no",
        )
    console.print(table)


# ---- lab --------------------------------------------------------------------

audit_app = typer.Typer(help="Data audits over the lake", no_args_is_help=True)
app.add_typer(audit_app, name="audit")


@audit_app.command("statements")
def audit_statements_cmd(
    tickers: str | None = typer.Option(
        None, "--tickers", help="comma-separated tickers; default every ticker with statements"
    ),
) -> None:
    """Check the three statements against each other (BL-36) and replace
    the audited tickers' rows in statement_flags. Tolerances: \\[audit]."""
    settings = _settings()
    with _open_lake(settings.lake.path) as lake:
        lake.migrate()
        report = _audit(settings, lake, _parse_tickers(tickers) or None)
    _print_audit(report)


lab_app = typer.Typer(help="Strategy lab: tune, fit and run survival tests")
app.add_typer(lab_app, name="lab")


@lab_app.command(
    "ic",
    context_settings={
        "allow_extra_args": True,
        "ignore_unknown_options": True,
        "help_option_names": [],
    },
    add_help_option=False,
)
def lab_ic(ctx: typer.Context) -> None:
    """Signal IC (and with --events an event study) of a strategy (BL-33/34):
    --strategy ID --tickers A,B [--params JSON --start --end --horizons 1,5,20
    --events --json F --html F]; ``stonks lab ic --help`` for all options."""
    from stonks.lab import signal_eval

    raise typer.Exit(code=signal_eval.main(list(ctx.args), prog="stonks lab ic"))


_LAB_TUNERS = ("grid", "random")
_LAB_OBJECTIVES = ("sharpe", "cagr", "final_return", "cv_sharpe", "cv_cagr", "cv_final_return")
_LAB_COST_MODELS = ("config", "zero", "realistic")


def _choice(flag: str, choices: tuple[str, ...]):
    def check(value: str | None) -> str | None:
        if value is not None and value not in choices:
            raise typer.BadParameter(f"{flag} must be one of {list(choices)}, got {value!r}")
        return value

    return check


def _preset_choices() -> tuple[str, ...]:
    from stonks.lab.survival.registry import preset_names

    return tuple(preset_names())


def _lab_suite(
    tests: str | None,
    preset: str | None,
    *,
    mcpt: bool,
    walk_forward: bool,
    registers: bool = False,
) -> list[str]:
    """Survival test ids for ``stonks lab run``: ``--tests``, else
    ``--preset``, else ``promotion`` when registering and ``quick``
    otherwise (the registry's ``resolve_suite``), plus ``mcpt`` /
    ``walk_forward`` when their flags are set."""
    from stonks.lab.survival.registry import resolve_suite, survival_test_names

    explicit = _parse_tickers(tests)
    known = survival_test_names()
    unknown = [t for t in explicit if t not in known]
    if unknown:
        raise typer.BadParameter(f"unknown tests {unknown}; choose from {known}")
    default = "promotion" if registers else "quick"
    suite = resolve_suite(explicit, preset=preset, default=default)
    for flag, test_id in ((mcpt, "mcpt"), (walk_forward, "walk_forward")):
        if flag and test_id not in suite:
            suite.append(test_id)
    return suite


def _parse_test_options(values: list[str] | None) -> dict[str, dict[str, Any]] | None:
    """``--test-option TEST.OPTION=VALUE`` (repeatable) as
    ``{test: {option: value}}``. VALUE is JSON when it parses (numbers,
    booleans, lists, null), else a plain string. The lab request validates
    names and values against each test's options."""
    import json

    out: dict[str, dict[str, Any]] = {}
    for raw in values or []:
        key, sep, text = raw.partition("=")
        test, dot, option = key.strip().partition(".")
        if not (sep and dot and test and option):
            raise typer.BadParameter(
                f"expected TEST.OPTION=VALUE (e.g. oos.min_trades=0), got {raw!r}",
                param_hint="--test-option",
            )
        try:
            value: Any = json.loads(text)
        except json.JSONDecodeError:
            value = text
        out.setdefault(test, {})[option.strip()] = value
    return out or None


_TEST_OPTION = typer.Option(
    None,
    "--test-option",
    help="survival-test option TEST.OPTION=VALUE, repeatable "
    "(e.g. oos.mode=sharpe, pbo.max_pbo=0.3, mc_trades.n_paths=2000); "
    "VALUE is JSON or a string",
)
_BENCHMARK = typer.Option(
    None,
    "--benchmark",
    help="auto|EW|<ticker>|none; default \\[lab] benchmark",
)


def _validated_strategy(strategy: str, params: str) -> tuple[type, dict]:
    import json

    from stonks.lab.catalog import resolve_strategy

    try:
        strategy_cls = resolve_strategy(strategy)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="STRATEGY") from None
    try:
        pinned = json.loads(params)
    except json.JSONDecodeError as exc:
        raise typer.BadParameter(f"not valid JSON ({exc})", param_hint="--params") from None
    if not isinstance(pinned, dict):
        raise typer.BadParameter("must be a JSON object", param_hint="--params")
    try:
        strategy_cls(pinned)  # validates names, types and bounds
    except (ValueError, TypeError) as exc:
        raise typer.BadParameter(str(exc), param_hint="--params") from None
    return strategy_cls, pinned


def _cost_model_option(name: str) -> str | None:
    """``config`` means ``[backtest.costs]`` (no override)."""
    return None if name == "config" else name


def _parallel_settings(settings: Settings, workers: int | None):
    return (
        settings.lab.parallel
        if workers is None
        else settings.lab.parallel.model_copy(update={"max_workers": workers})
    )


@lab_app.command("run")
def lab_run(
    strategy: str = typer.Argument(
        ..., help="strategy id, class name or module:Class (see stonks.lab.catalog)"
    ),
    params: str = typer.Option(
        "{}",
        "--params",
        help="JSON object of params to pin; tunable params left out are tuned",
    ),
    tickers: str | None = typer.Option(
        None, "--tickers", help="comma-separated universe; default \\[production].universe"
    ),
    universe_id: str | None = typer.Option(
        None,
        "--universe-id",
        help="stored universe: every member during the window, delisted names included",
    ),
    ensure_data: bool = typer.Option(
        False, "--ensure-data", help="fetch missing bars first (\\[ensure] settings, --source)"
    ),
    source_id: str = typer.Option(
        DEFAULT_SOURCE_ID,
        "--source",
        help=f"source for --ensure-data ({'|'.join(SOURCE_IDS)})",
        callback=_validate_source,
    ),
    start: str = typer.Option(..., "--start", callback=_validate_iso_date, help="YYYY-MM-DD"),
    end: str = typer.Option(..., "--end", callback=_validate_iso_date, help="YYYY-MM-DD"),
    interval: str = typer.Option("1d", "--interval", help="bar interval (1d, 1h, 5m, ...)"),
    train_ratio: float = typer.Option(0.7, "--train-ratio", min=0.05, max=0.95),
    tuner: str = typer.Option(
        "grid", "--tuner", callback=_choice("--tuner", _LAB_TUNERS), help="grid|random"
    ),
    grid_size: int = typer.Option(5, "--grid-size", min=1, help="points per numeric axis"),
    budget: int = typer.Option(20, "--budget", min=1, help="tuning trials"),
    seed: int = typer.Option(0, "--seed", help="tuner seed"),
    objective: str = typer.Option(
        "sharpe",
        "--objective",
        callback=_choice("--objective", _LAB_OBJECTIVES),
        help="sharpe|cagr|final_return, or cv_ plus one of them to score on purged folds",
    ),
    tests: str | None = typer.Option(
        None,
        "--tests",
        help="comma-separated survival test ids (see stonks.lab.survival.registry); "
        "default: --preset",
    ),
    preset: str | None = typer.Option(
        None,
        "--preset",
        callback=_choice("--preset", _preset_choices()),
        help="named suite when --tests is not given: quick (default)|standard|promotion",
    ),
    mcpt: bool = typer.Option(False, "--mcpt", help="out-of-sample permutation test"),
    mcpt_retune: bool = typer.Option(
        False, "--mcpt-retune", help="permutation test re-tuning on each permuted train window"
    ),
    mcpt_permutations: int | None = typer.Option(
        None, "--mcpt-permutations", min=1, help="default 200"
    ),
    mcpt_max_p: float | None = typer.Option(
        None, "--mcpt-max-p", min=0.0001, max=1.0, help="default 0.05"
    ),
    mcpt_seed: int | None = typer.Option(None, "--mcpt-seed", help="default 17"),
    walk_forward: bool = typer.Option(False, "--walk-forward", help="add walk-forward test"),
    wf_splits: int | None = typer.Option(
        None, "--wf-splits", min=1, help="default \\[lab.walk_forward].n_splits"
    ),
    wf_test_days: int | None = typer.Option(
        None, "--wf-test-days", min=1, help="default \\[lab.walk_forward].test_days"
    ),
    wf_anchored: bool | None = typer.Option(
        None, "--wf-anchored/--wf-rolling", help="default \\[lab.walk_forward].anchored"
    ),
    wf_min_wfe: float | None = typer.Option(
        None,
        "--wf-min-wfe",
        min=0.0,
        max=1.0,
        help="walk-forward efficiency gate; default \\[lab.walk_forward].min_wfe",
    ),
    wf_matrix: bool | None = typer.Option(
        None,
        "--wf-matrix/--no-wf-matrix",
        help="also run the train x test matrix; default \\[lab.walk_forward].matrix",
    ),
    embargo_bars: int | None = typer.Option(
        None,
        "--embargo-bars",
        min=0,
        help="bars between train and validation windows; default \\[lab] embargo_bars",
    ),
    cost_model: str = typer.Option(
        "config",
        "--cost-model",
        callback=_choice("--cost-model", _LAB_COST_MODELS),
        help="config (\\[backtest.costs], default)|zero|realistic",
    ),
    workers: int | None = typer.Option(
        None, "--workers", min=0, help="tuning processes; default \\[lab.parallel] (0 = all cores)"
    ),
    hypothesis: str | None = typer.Option(
        None, "--hypothesis", help="the edge and who pays for it (recorded before tuning, P1)"
    ),
    premortem: str | None = typer.Option(
        None, "--premortem", help="how this strategy is expected to fail (recorded)"
    ),
    register_if_passes: bool = typer.Option(
        False,
        "--register-if-passes",
        "--register",
        help="register the strategy in shadow only if every survival test passes "
        "(default suite: promotion)",
    ),
    test_option: list[str] | None = _TEST_OPTION,
    benchmark: str | None = _BENCHMARK,
    strict: bool = typer.Option(
        False, "--strict", help="treat data preflight warnings as errors (\\[lab] strict_preflight)"
    ),
    no_preflight: bool = typer.Option(
        False, "--no-preflight", help="skip the data preflight (\\[lab] preflight)"
    ),
    json_out: str | None = typer.Option(None, "--json-out", help="write the result as JSON"),
) -> None:
    """Tune a strategy on the train window, then run the survival suite.

    Every run is pre-registered in the trial ledger (with --hypothesis /
    --premortem). Transaction costs come from \\[backtest.costs] unless
    --cost-model says otherwise; walk-forward defaults from
    \\[lab.walk_forward]; tuning workers from \\[lab.parallel]; the benchmark
    from \\[lab] benchmark. --test-option tunes any survival test.
    """
    import json

    from stonks.app.errors import ValidationError as AppValidationError
    from stonks.app.lab import (
        LabRunRequest,
        McptOptions,
        build_data_ensurer,
        execute_lab_run,
    )
    from stonks.app.serialize import to_jsonable
    from stonks.app.strategies import StrategyRef
    from stonks.core.interval import Interval

    # -- validate everything before touching the lake ------------------------
    strategy_cls, pinned = _validated_strategy(strategy, params)
    start_d, end_d = date.fromisoformat(start), date.fromisoformat(end)
    if start_d >= end_d:
        raise typer.BadParameter("--start must be before --end")
    try:
        bar_interval = Interval.parse(interval)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--interval") from None
    if mcpt and mcpt_retune:
        raise typer.BadParameter("pass one of --mcpt / --mcpt-retune, not both")
    suite = _lab_suite(
        tests,
        preset,
        mcpt=mcpt or mcpt_retune,
        walk_forward=walk_forward,
        registers=register_if_passes,
    )
    test_options = _parse_test_options(test_option)
    # only what was passed: the rest comes from the preset or the test
    mcpt_set: dict[str, Any] = {
        k: v
        for k, v in {
            "n_permutations": mcpt_permutations,
            "max_p_value": mcpt_max_p,
            "seed": mcpt_seed,
            "retune": True if mcpt_retune else False if mcpt else None,
        }.items()
        if v is not None
    }

    settings = _settings()
    universe = _parse_tickers(tickers)
    if not universe and not universe_id:
        configured = settings.production.universe
        if isinstance(configured, str):
            universe_id = configured
        else:
            universe = list(configured)
    if not universe and not universe_id:
        raise typer.BadParameter("pass --tickers or --universe-id, or set [production].universe")

    wf_overrides = {
        k: v
        for k, v in {
            "n_splits": wf_splits,
            "test_days": wf_test_days,
            "anchored": wf_anchored,
            "min_wfe": wf_min_wfe,
            "matrix": wf_matrix,
        }.items()
        if v is not None
    }
    class_path = f"{strategy_cls.__module__}:{strategy_cls.__name__}"
    try:
        request = LabRunRequest(
            strategy=StrategyRef(class_path=class_path),
            universe=universe,
            universe_id=universe_id,
            ensure_data=ensure_data,
            start=start_d,
            end=end_d,
            interval=bar_interval.code,
            train_ratio=train_ratio,
            tuner=tuner,  # type: ignore[arg-type]
            grid_size=grid_size,
            budget=budget,
            seed=seed,
            objective=objective,  # type: ignore[arg-type]
            survival_tests=suite,
            walk_forward=(
                settings.lab.walk_forward.model_copy(update=wf_overrides)
                if "walk_forward" in suite
                else None
            ),
            # the preset's test options apply unless --tests replaced its suite
            preset=None if tests else preset or ("promotion" if register_if_passes else "quick"),
            mcpt=McptOptions(**mcpt_set) if "mcpt" in suite and mcpt_set else None,
            register_if_passes=register_if_passes,
            cost_model=_cost_model_option(cost_model),  # type: ignore[arg-type]
            hypothesis=hypothesis,
            premortem=premortem,
            test_options=test_options,
            benchmark=benchmark,
            embargo_bars=embargo_bars,
            preflight=False if no_preflight else None,
            strict_preflight=True if strict else None,
        )
    except ValueError as exc:  # pydantic ValidationError is a ValueError
        raise typer.BadParameter(str(exc)) from None

    ensure_source = None
    if ensure_data:
        from stonks.universes import commands as universe_commands

        ensure_source = universe_commands.ensure_source(settings, source_id)
    lake = _open_lake(settings.lake.path)
    state = SqliteState(settings.state.path)
    try:
        state.migrate()
        if universe_id is not None and universe_id not in lake.universe_ids():
            raise typer.BadParameter(
                f"universe {universe_id!r} has no members: create and refresh it first "
                f"(stonks universe refresh {universe_id})",
                param_hint="--universe-id",
            )
        execution = execute_lab_run(
            settings,
            strategy_cls,
            request,
            lake=lake,
            state=state,
            fixed_params=pinned,
            parallel=_parallel_settings(settings, workers),
            data_ensurer=(
                build_data_ensurer(settings, lake, ensure_source)
                if ensure_source is not None
                else None
            ),
        )
    except AppValidationError as exc:  # e.g. [lab] embargo_bars vs the window
        raise typer.BadParameter(str(exc)) from None
    finally:
        lake.close()
        state.close()
    result, registered_id = execution.result, execution.registered_id
    preflight = execution.view().preflight
    for issue in preflight.issues if preflight else []:
        console.print(f"[yellow]preflight {issue.severity} [{issue.code}][/yellow] {issue.message}")

    table = Table(title=f"lab run: {strategy_cls.__name__}  verdict={result.verdict}")
    for col in ("test", "passed", "metrics"):
        table.add_column(col)
    for rep in result.survival_reports:
        metrics = ", ".join(f"{k}={v:.4g}" for k, v in sorted(rep.metrics.items()))
        table.add_row(rep.test_id, "yes" if rep.passed else "no", metrics)
    console.print(f"run: {result.run_id}")
    console.print(f"best params: {dict(result.best_params)}")
    console.print(
        f"best {objective} (train): {result.best_score:.4g}  "
        f"[{result.n_trials_run} trials, {result.n_trials_class} for this class]"
    )
    console.print(table)
    if execution.benchmark is not None:
        s = execution.benchmark.stats
        console.print(
            f"vs {execution.benchmark.curve.name} (validation): "
            f"excess CAGR {s.excess_cagr:+.2%}, IR {s.information_ratio:.2f}, "
            f"beta {s.beta:.2f}, alpha t {s.alpha_tstat:.2f}"
        )
    colour = "green" if result.verdict == "pass" else "red"
    console.print(f"[{colour}]verdict: {result.verdict}[/{colour}]")
    if register_if_passes:
        if registered_id is not None:
            console.print(f"[green]registered {registered_id} (shadow)[/green]")
        else:
            console.print("[yellow]not registered: verdict is fail[/yellow]")

    if json_out is not None:
        doc = {
            "strategy": class_path,
            "universe": universe,
            "universe_id": universe_id,
            "window": [start, end],
            "interval": bar_interval.code,
            "run_id": result.run_id,
            "n_trials_run": result.n_trials_run,
            "n_trials_class": result.n_trials_class,
            "hypothesis": hypothesis,
            "best_params": dict(result.best_params),
            "best_score": result.best_score,
            "verdict": result.verdict,
            "survival_reports": [
                {
                    "test_id": rep.test_id,
                    "passed": rep.passed,
                    "metrics": dict(rep.metrics),
                    "notes": rep.notes,
                }
                for rep in result.survival_reports
            ],
            "registered_id": registered_id,
            "benchmark": execution.view().benchmark,
            "preflight": preflight,
        }
        Path(json_out).write_text(json.dumps(to_jsonable(doc), indent=2, sort_keys=True))


@lab_app.command("sweep")
def lab_sweep(
    tickers: str | None = typer.Option(
        None, "--tickers", help="comma-separated basket; default \\[production].universe"
    ),
    start: str = typer.Option(..., "--start", callback=_validate_iso_date, help="YYYY-MM-DD"),
    end: str = typer.Option(..., "--end", callback=_validate_iso_date, help="YYYY-MM-DD"),
    strategies: str | None = typer.Option(
        None,
        "--strategies",
        help="comma-separated strategy ids (default: every catalogued non-wrapper strategy)",
    ),
    exclude: str | None = typer.Option(None, "--exclude", help="comma-separated ids to skip"),
    interval: str = typer.Option("1d", "--interval", help="bar interval (1d, 1h, 5m, ...)"),
    train_ratio: float = typer.Option(0.7, "--train-ratio", min=0.05, max=0.95),
    tuner: str = typer.Option(
        "random", "--tuner", callback=_choice("--tuner", _LAB_TUNERS), help="grid|random"
    ),
    grid_size: int = typer.Option(5, "--grid-size", min=1, help="points per numeric axis"),
    budget: int = typer.Option(10, "--budget", min=1, help="tuning trials per run"),
    seed: int = typer.Option(0, "--seed", help="tuner seed"),
    objective: str = typer.Option(
        "sharpe",
        "--objective",
        callback=_choice("--objective", _LAB_OBJECTIVES),
        help="sharpe|cagr|final_return, or cv_ plus one of them to score on purged folds",
    ),
    tests: str | None = typer.Option(
        None, "--tests", help="comma-separated survival test ids; default: --preset"
    ),
    preset: str | None = typer.Option(
        None,
        "--preset",
        callback=_choice("--preset", _preset_choices()),
        help="named suite when --tests is not given: quick (default)|standard|promotion",
    ),
    cost_model: str = typer.Option(
        "config",
        "--cost-model",
        callback=_choice("--cost-model", _LAB_COST_MODELS),
        help="config (\\[backtest.costs], default)|zero|realistic",
    ),
    workers: int | None = typer.Option(
        None, "--workers", min=0, help="processes; default \\[lab.parallel] (0 = all cores)"
    ),
    test_option: list[str] | None = _TEST_OPTION,
    benchmark: str | None = _BENCHMARK,
    csv_out: str | None = typer.Option(None, "--csv-out", help="write the summary as CSV"),
    json_out: str | None = typer.Option(None, "--json-out", help="write the summary as JSON"),
) -> None:
    """Run every catalogued strategy (or --strategies) through the lab on a
    ticker basket, in parallel, and summarise the verdicts.

    Single-ticker strategies get one run per basket ticker, universe-aware
    ones one run over the basket. Every run is recorded in the trial
    ledger; nothing is registered.
    """
    from stonks.app.lab import LabRunRequest
    from stonks.app.strategies import StrategyRef
    from stonks.app.sweep import plan_sweep, run_sweep, write_csv, write_json
    from stonks.core.interval import Interval

    start_d, end_d = date.fromisoformat(start), date.fromisoformat(end)
    if start_d >= end_d:
        raise typer.BadParameter("--start must be before --end")
    try:
        bar_interval = Interval.parse(interval)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--interval") from None
    suite = _lab_suite(tests, preset, mcpt=False, walk_forward=False)

    settings = _settings()
    basket = _parse_tickers(tickers)
    if not basket:
        from stonks.production.universe import EmptyUniverseError, window_tickers

        with _open_lake(settings.lake.path) as lake:
            try:
                basket = window_tickers(lake, settings.production.universe, start_d, end_d)
            except EmptyUniverseError as exc:
                raise typer.BadParameter(str(exc), param_hint="--tickers") from None
    try:
        tasks = plan_sweep(basket, _parse_tickers(strategies), _parse_tickers(exclude))
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--strategies") from None
    if not tasks:
        raise typer.BadParameter("no strategies left to sweep", param_hint="--strategies")
    try:
        request = LabRunRequest(
            strategy=StrategyRef(class_path=tasks[0].class_path),
            universe=basket,
            start=start_d,
            end=end_d,
            interval=bar_interval.code,
            train_ratio=train_ratio,
            tuner=tuner,  # type: ignore[arg-type]
            grid_size=grid_size,
            budget=budget,
            seed=seed,
            objective=objective,  # type: ignore[arg-type]
            survival_tests=suite,
            walk_forward=settings.lab.walk_forward if "walk_forward" in suite else None,
            cost_model=_cost_model_option(cost_model),  # type: ignore[arg-type]
            test_options=_parse_test_options(test_option),
            benchmark=benchmark,
        )
    except ValueError as exc:  # pydantic ValidationError is a ValueError
        raise typer.BadParameter(str(exc)) from None
    parallel = _parallel_settings(settings, workers)
    console.print(
        f"sweeping {len(tasks)} lab runs ({len({t.strategy_id for t in tasks})} strategies, "
        f"{len(basket)} tickers) on {parallel.resolved_workers()} worker(s)"
    )
    lake = _open_lake(settings.lake.path)
    try:
        rows = run_sweep(settings, tasks, request, lake=lake, parallel=parallel)
    finally:
        lake.close()

    table = Table(title=f"lab sweep: {len(rows)} runs")
    for col in ("strategy", "ticker", "verdict", "score", "failed tests", "best params"):
        table.add_column(col)
    for row in rows:
        colour = {"pass": "green", "fail": "red"}.get(row.verdict, "yellow")
        failed = [t for t, rep in row.survival.items() if not rep["passed"]]
        table.add_row(
            row.strategy,
            row.ticker or "*",
            f"[{colour}]{row.verdict}[/{colour}]",
            "-" if row.best_score is None else f"{row.best_score:.3g}",
            ",".join(failed) if row.verdict != "error" else (row.error or "")[:60],
            ",".join(f"{k}={v}" for k, v in sorted(row.best_params.items()) if k != "ticker"),
        )
    console.print(table)
    passed = sum(r.verdict == "pass" for r in rows)
    errors = sum(r.verdict == "error" for r in rows)
    console.print(f"{passed} pass, {len(rows) - passed - errors} fail, {errors} error")
    if csv_out is not None:
        write_csv(rows, Path(csv_out))
        console.print(f"wrote {csv_out}")
    if json_out is not None:
        write_json(rows, Path(json_out))
        console.print(f"wrote {json_out}")


# ---- go-live gate (4.3) -----------------------------------------------------

# ---- risk halts and the kill switch ------------------------------------------

halts_app = typer.Typer(help="Kill switch and risk halts", no_args_is_help=True)
app.add_typer(halts_app, name="halts")

_HALT_USER = typer.Option(
    None,
    "--user",
    help="act as this user (email or id); default: the operator (service:cli)",
)


def _halt_scope(context: Any, user: str | None) -> Any:
    """``service:cli`` (admin rights over every book), or the named user."""
    from stonks.accounts import NotFound, Scope, UserRepository

    if user is None:
        return Scope.service("cli")
    with context.state() as state:
        users = UserRepository(state)
        try:
            found = users.get_by_email(user) if "@" in user else users.get(user)
        except NotFound:
            raise typer.BadParameter(f"no user {user!r}", param_hint="--user") from None
    return Scope.for_user(found)


def _halt_call(fn: Any) -> Any:
    """Run a HaltService call, turning its errors into a usage error."""
    from stonks.app.errors import AppError

    try:
        return fn()
    except AppError as exc:
        raise typer.BadParameter(str(exc)) from None
    except ValueError as exc:  # request model validation
        raise typer.BadParameter(str(exc)) from None


def _halt_service() -> tuple[Any, Any]:
    from stonks.app.context import AppContext
    from stonks.app.halts import HaltService

    context = AppContext(_settings())
    with context.state() as state:
        state.migrate()
    return context, HaltService(context)


def _print_halts(views: list[Any]) -> None:
    table = Table(title="risk halts")
    for col in ("id", "kind", "target", "halt", "reason", "tripped", "expires", "state"):
        table.add_column(col)
    for h in views:
        target = (
            "global"
            if h.scope == "global"
            else f"user {h.user_id}"
            if h.scope == "user"
            else f"portfolio {h.portfolio_id}"
        )
        state = (
            "active" if h.active else f"cleared by {h.cleared_by}" if h.cleared_at else "expired"
        )
        table.add_row(
            str(h.id),
            h.kind,
            target,
            h.halt,
            h.reason,
            f"{h.tripped_at} by {h.tripped_by}",
            h.expires_on.isoformat() if h.expires_on else "-",
            state,
        )
    console.print(table)


@halts_app.command("list")
def halts_list(
    include_cleared: bool = typer.Option(False, "--all", help="also cleared and expired halts"),
    user: str | None = _HALT_USER,
) -> None:
    """Halts in force today (with --all, every halt), newest first."""
    context, service = _halt_service()
    views = _halt_call(
        lambda: service.list(_halt_scope(context, user), include_cleared=include_cleared)
    )
    if not views:
        console.print("no halt in force" if not include_cleared else "no halts")
        return
    _print_halts(views)


@halts_app.command("kill")
def halts_kill(
    scope: str = typer.Option(
        ...,
        "--scope",
        callback=_choice("--scope", ("global", "user", "portfolio")),
        help="global (every portfolio) | user (every portfolio of --user) | portfolio",
    ),
    portfolio: str | None = typer.Option(
        None, "--portfolio", help="portfolio id (--scope portfolio)"
    ),
    flatten: bool = typer.Option(
        False,
        "--flatten",
        help="only stops buys: sells and exits still go through, and no position is closed",
    ),
    reason: str = typer.Option(..., "--reason", help="why (audited)"),
    user: str | None = _HALT_USER,
) -> None:
    """Engage the kill switch: no new orders. With --flatten it only stops
    buys and leaves open positions as they are."""
    from stonks.app.halts import KillSwitchRequest

    context, service = _halt_service()
    who = _halt_scope(context, user)
    view = _halt_call(
        lambda: service.engage_kill(
            who,
            KillSwitchRequest(
                scope=scope,  # type: ignore[arg-type]
                portfolio_id=portfolio,
                flatten=flatten,
                reason=reason,
            ),
        )
    )
    console.print(f"[red]kill switch on[/red]: halt #{view.id} ({view.scope}, {view.halt})")


@halts_app.command("resume")
def halts_resume(
    halt_id: int = typer.Argument(..., help="the kill switch's halt id"),
    reason: str = typer.Option(..., "--reason", help="why trading may resume (audited)"),
    user: str | None = _HALT_USER,
) -> None:
    """Turn a kill switch off. Asks you to type RESUME TRADING."""
    from stonks.app.halts import RESUME_PHRASE, ResumeRequest

    context, service = _halt_service()
    who = _halt_scope(context, user)
    typed = typer.prompt(f"Type {RESUME_PHRASE} to resume trading")
    view = _halt_call(
        lambda: service.resume_kill(who, halt_id, ResumeRequest(confirmation=typed, reason=reason))
    )
    console.print(f"[green]trading resumed[/green]: halt #{view.id} cleared")


@halts_app.command("clear")
def halts_clear(
    halt_id: int = typer.Argument(..., help="a circuit-breaker or operational halt id"),
    reason: str = typer.Option(..., "--reason", help="why it may be cleared (audited)"),
    user: str | None = _HALT_USER,
) -> None:
    """The logged reset of a circuit-breaker or operational halt."""
    from stonks.app.halts import ClearHaltRequest

    context, service = _halt_service()
    who = _halt_scope(context, user)
    view = _halt_call(lambda: service.clear(who, halt_id, ClearHaltRequest(reason=reason)))
    console.print(f"[green]cleared[/green]: halt #{view.id} ({view.kind})")


# ---- transaction cost analysis and the journal (BL-32) ----------------------

from stonks.cli_tca import app as tca_app  # noqa: E402

app.add_typer(tca_app, name="tca")

golive_app = typer.Typer(help="Go-live gate for paper-traded strategies")
app.add_typer(golive_app, name="golive")


@golive_app.command("check")
def golive_check(
    strategy_id: str,
    since: str | None = typer.Option(
        None,
        "--since",
        help="start the paper period on this day (YYYY-MM-DD); default: all of it",
        callback=_validate_iso_date,
    ),
) -> None:
    """Evaluate a strategy's paper period against \\[golive]. Exit code 1 when
    any check fails. Never changes the strategy's status."""
    from stonks.production.golive import evaluate_golive

    settings = _settings()
    since_d = date.fromisoformat(since) if since else None
    state, registry = _open_registry(settings)
    try:
        report = evaluate_golive(state, registry, strategy_id, settings.golive, since=since_d)
    except KeyError:
        console.print(f"[red]no strategy with id {strategy_id!r}[/red]")
        raise typer.Exit(code=1) from None
    finally:
        state.close()

    table = Table(title=f"go-live gate: {strategy_id} ({report.status}, {report.source} P&L)")
    for col in ("check", "status", "detail"):
        table.add_column(col)
    for c in report.checks:
        table.add_row(c.name, "[green]PASS[/green]" if c.passed else "[red]FAIL[/red]", c.detail)
    console.print(table)
    console.print("promotion checklist (context, not checks):")
    for key, value in report.checklist.items():
        shown = "-" if value is None else f"{value:.4g}" if isinstance(value, float) else value
        console.print(f"  {key}: {shown}", markup=False)
    if report.costs:
        c = report.costs
        console.print(
            f"live costs (TCA): {c['orders']} order(s), shortfall "
            + " vs model ".join(
                "-" if v is None else f"{v:.1f} bps" for v in (c["live_is_bps"], c["modelled_bps"])
            ),
            markup=False,
        )
    if report.passed:
        console.print("[green]PASS[/green]: ready for a human to promote")
        return
    console.print(f"[red]FAIL[/red]: {len(report.failures)} check(s) failed")
    raise typer.Exit(code=1)


# ---- static HTML report (4.1) -----------------------------------------------


_REPORT_OUT = typer.Option(
    Path("data/reports/report.html"), "--out", help="where to write the HTML file"
)
_REPORT_STRATEGIES = typer.Option(
    None, "--strategy", help="only these strategy ids (repeatable); default: all"
)


def _write_tear_sheet(
    out: Path,
    target: str,
    start: str | None,
    end: str | None,
    tickers: str | None,
    params: str,
    benchmark: str | None,
) -> None:
    import json

    from stonks.app.catalog import CatalogService
    from stonks.app.context import AppContext
    from stonks.app.errors import AppError
    from stonks.app.services import default_strategy_sources
    from stonks.app.tearsheets import (
        TearSheetWindow,
        render_backtest_tear_sheet,
        tear_sheet_request,
    )

    try:
        pinned = json.loads(params)
    except json.JSONDecodeError as exc:
        raise typer.BadParameter(f"not valid JSON ({exc})", param_hint="--params") from None
    if not isinstance(pinned, dict):
        raise typer.BadParameter("must be a JSON object", param_hint="--params")
    context = AppContext(_settings())
    window = TearSheetWindow(
        start=date.fromisoformat(start) if start else None,
        end=date.fromisoformat(end) if end else None,
        universe=tuple(_parse_tickers(tickers)),
        params=pinned,
        benchmark=benchmark,
    )
    try:
        request = tear_sheet_request(context, target, window)
        html = render_backtest_tear_sheet(
            context, request, CatalogService(default_strategy_sources())
        )
    except AppError as exc:
        raise typer.BadParameter(str(exc), param_hint="--backtest") from None
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    console.print(f"[green]wrote[/green] {out}")


@app.command("report")
def report(
    out: Path = _REPORT_OUT,
    strategies: list[str] | None = _REPORT_STRATEGIES,
    since: str | None = typer.Option(
        None,
        "--since",
        help="first day to show (YYYY-MM-DD); returns are still measured from inception",
        callback=_validate_iso_date,
    ),
    backtest: str | None = typer.Option(
        None,
        "--backtest",
        help="write a backtest tear sheet instead: a backtest job id, or a strategy id / "
        "catalog name (with --start/--end)",
    ),
    start: str | None = typer.Option(
        None, "--start", callback=_validate_iso_date, help="tear sheet start (YYYY-MM-DD)"
    ),
    end: str | None = typer.Option(
        None, "--end", callback=_validate_iso_date, help="tear sheet end (YYYY-MM-DD)"
    ),
    tickers: str | None = typer.Option(
        None, "--tickers", help="tear sheet universe; default \\[production].universe"
    ),
    params: str = typer.Option(
        "{}", "--params", help="JSON params for a catalog strategy's tear sheet"
    ),
    benchmark: str | None = _BENCHMARK,
) -> None:
    """Write a self-contained static HTML report: equity curve, drawdown,
    positions, orders/fills, and per-strategy verdicts, shadow P&L and drift.

    With --backtest, write a backtest tear sheet (strategy vs benchmark,
    drawdowns, rolling Sharpe, monthly returns, trade and benchmark stats)."""
    from stonks.reporting import build_report, render_html

    if backtest is not None:
        _write_tear_sheet(out, backtest, start, end, tickers, params, benchmark)
        return
    settings = _settings()
    since_d = date.fromisoformat(since) if since else None
    state, registry = _open_registry(settings)
    try:
        data = build_report(
            state, registry, settings.golive, strategy_ids=strategies or None, since=since_d
        )
    finally:
        state.close()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_html(data), encoding="utf-8")
    console.print(f"[green]wrote[/green] {out}")


# ---- MCP server -------------------------------------------------------------


@app.command("mcp")
def mcp_server() -> None:
    """Run the MCP server over stdio for Claude Code / Claude Desktop.

    A client of the running REST API (\\[mcp].api_url, start it with
    `stonks serve`); it never opens the lake. Write and job tools send
    STONKS_API_TOKEN. See docs/mcp.md.
    """
    from stonks.mcp.entry import McpConfigError, run

    try:
        run(_settings())
    except McpConfigError as exc:
        # stdout belongs to the MCP protocol; report on stderr.
        typer.echo(f"stonks mcp: {exc}", err=True)
        raise typer.Exit(code=2) from None


if __name__ == "__main__":
    app()

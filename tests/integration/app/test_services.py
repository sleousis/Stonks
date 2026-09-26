"""Service layer: every service against a seeded tmp lake + state."""

from __future__ import annotations

import ast
import time
from datetime import date
from pathlib import Path

import pytest

import stonks.app
from stonks.app.catalog import CatalogService, PackageStrategySource
from stonks.app.errors import ConfigurationError, NotFoundError, ValidationError
from stonks.app.ingest import IngestRequest
from stonks.app.lab import BacktestRequest, LabRunRequest
from stonks.app.strategies import StrategyRef
from stonks.app.ticks import TickRequest

# ---- layering ---------------------------------------------------------------


def test_app_package_never_imports_web_frameworks():
    root = Path(stonks.app.__file__).parent
    offenders = []
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                if name.split(".")[0] in {"fastapi", "starlette", "uvicorn", "stonks"} and (
                    name.split(".")[0] != "stonks" or name.startswith("stonks.api")
                ):
                    offenders.append(f"{path.name}: {name}")
    assert offenders == []


# ---- portfolio --------------------------------------------------------------


def test_services_build_the_auth_service_from_the_auth_section(settings, seeded):
    from stonks.app.context import AppContext
    from stonks.app.services import Services
    from stonks.auth import AuthService

    settings.auth = settings.auth.model_copy(update={"session_idle_hours": 3.0})
    svc = Services.create(AppContext(settings))
    assert isinstance(svc.auth, AuthService)
    assert svc.auth.settings.session_idle_hours == 3.0


def test_portfolio_current_values_positions_at_latest_prices(services):
    view = services.portfolio.current()
    assert view.tick_id is not None
    assert [p.ticker for p in view.positions] == ["UP.US"]
    pos = view.positions[0]
    assert pos.quantity > 0
    # latest close in the seeded lake is 200 (2026-04-01)
    assert pos.price == pytest.approx(200.0)
    assert pos.price_date == date(2026, 4, 1)
    assert pos.market_value == pytest.approx(pos.quantity * 200.0)
    assert view.total_value == pytest.approx(view.cash + pos.market_value)
    assert view.positions_value == pytest.approx(pos.market_value)
    assert pos.weight == pytest.approx(pos.market_value / view.total_value)


def test_portfolio_without_snapshot_is_seeded_from_initial_cash(settings, tmp_path):
    from stonks.app.context import AppContext
    from stonks.app.services import Services

    svc = Services.create(AppContext(settings))
    svc.start()
    try:
        view = svc.portfolio.current()
    finally:
        svc.shutdown()
    assert view.taken_at is None
    assert view.positions == []
    assert view.cash == settings.production.initial_cash


def test_portfolio_snapshot_history_paginates(services):
    page = services.portfolio.snapshots(limit=10, offset=0)
    assert page.total == 1
    assert page.items[0].positions["UP.US"] > 0


# ---- strategies -------------------------------------------------------------


def test_strategy_list_filters_by_status(services, seeded):
    all_ = services.strategies.list(limit=10, offset=0)
    assert all_.total == 2
    active = services.strategies.list(status="active", limit=10, offset=0)
    assert [s.id for s in active.items] == [seeded["active_id"]]
    assert active.items[0].applicable_asset_classes == ["equity"]


def test_strategy_detail_has_params_and_reports(services, seeded):
    detail = services.strategies.get(seeded["active_id"])
    assert detail.params == {"ticker": "UP.US", "allocation": 0.5}
    assert detail.class_path.endswith(":BuyAndHold")
    assert [r.test_id for r in detail.survival_reports] == ["oos"]
    assert detail.survival_reports[0].metrics == {"sharpe_oos": 1.5}


def test_strategy_status_changes(services, seeded):
    sid = seeded["shadow_id"]
    override = "test override: no paper period in this fixture"
    assert services.strategies.promote(sid, override=True, reason=override).status == "active"
    assert services.strategies.retire(sid, reason="superseded").status == "retired"
    assert services.strategies.shadow(sid, reason="re-incubate").status == "shadow"
    assert len(services.strategies.history(sid)) == 3


def test_strategy_unknown_id_is_not_found(services):
    with pytest.raises(NotFoundError):
        services.strategies.get("missing")
    with pytest.raises(NotFoundError):
        services.strategies.promote("missing")


def test_strategy_invalid_status_filter(services):
    with pytest.raises(ValidationError):
        services.strategies.list(status="bogus", limit=10, offset=0)


def test_strategy_ref_resolves_registered_id_or_class_path(services, seeded):
    by_id = services.strategies.resolve(StrategyRef(strategy_id=seeded["active_id"]))
    assert by_id.params["ticker"] == "UP.US"
    by_class = services.strategies.resolve(
        StrategyRef(
            class_path="stonks.strategies.examples.buy_and_hold:BuyAndHold",
            params={"ticker": "FLAT.US"},
        )
    )
    assert by_class.params["ticker"] == "FLAT.US"


def test_strategy_ref_requires_exactly_one_source():
    with pytest.raises(ValueError):
        StrategyRef()
    with pytest.raises(ValueError):
        StrategyRef(strategy_id="x", class_path="a:B")


def test_strategy_ref_rejects_classes_outside_the_catalog(services):
    with pytest.raises(ValidationError):
        services.strategies.resolve(StrategyRef(class_path="os:system"))


def test_strategy_ref_rejects_invalid_params(services):
    with pytest.raises(ValidationError):
        services.strategies.resolve(
            StrategyRef(
                class_path="stonks.strategies.examples.momentum:Momentum",
                params={"lookback_days": 9999},
            )
        )


# ---- catalog ----------------------------------------------------------------


def test_catalog_lists_example_strategies_with_parameter_specs(services):
    classes = {c.class_path: c for c in services.catalog.strategies()}
    bah = classes["stonks.strategies.examples.buy_and_hold:BuyAndHold"]
    assert bah.name == "buy_and_hold"
    assert bah.source == "builtin"
    assert {p.name for p in bah.parameters} == {"ticker", "allocation"}
    assert "stonks.strategies.examples.momentum:Momentum" in classes


def test_catalog_sources_are_pluggable():
    class OneClassSource:
        name = "custom"

        def discover(self):
            from stonks.strategies.examples.momentum import Momentum

            return [Momentum]

    catalog = CatalogService(sources=[OneClassSource()])
    assert [c.class_path for c in catalog.strategies()] == [
        "stonks.strategies.examples.momentum:Momentum"
    ]
    assert catalog.strategies()[0].source == "custom"


def test_catalog_package_source_discovers_examples():
    found = PackageStrategySource("stonks.strategies.examples").discover()
    assert any(cls.__name__ == "BuyAndHold" for cls in found)


def test_catalog_intervals_and_asset_classes(services):
    codes = [i.code for i in services.catalog.intervals()]
    assert "1d" in codes and "5m" in codes
    assert services.catalog.asset_classes() == ["equity", "crypto", "commodity", "bond"]


# ---- market data ------------------------------------------------------------


def test_instrument_search(services):
    page = services.market.instruments(q="up", limit=10, offset=0)
    assert [i.id for i in page.items] == ["UP.US"]
    assert page.items[0].name == "Up Corp"
    by_class = services.market.instruments(asset_class="crypto", limit=10, offset=0)
    assert by_class.total == 0
    everything = services.market.instruments(limit=2, offset=0)
    assert everything.total == 3
    assert len(everything.items) == 2


def test_bars_window(services):
    series = services.market.bars(
        "UP.US", interval="1d", start=date(2026, 3, 30), end=date(2026, 4, 1)
    )
    assert series.interval == "1d"
    assert len(series.bars) == 3
    assert series.bars[-1].close == pytest.approx(200.0)
    assert series.truncated is False


def test_bars_limit_keeps_most_recent(services):
    series = services.market.bars("UP.US", interval="1d", limit=2)
    assert len(series.bars) == 2
    assert series.truncated is True
    assert series.bars[-1].timestamp.date() == date(2026, 4, 1)
    assert series.bars[0].timestamp < series.bars[1].timestamp


def test_bars_bad_interval_is_validation_error(services):
    with pytest.raises(ValidationError):
        services.market.bars("UP.US", interval="7x")


def test_coverage(services):
    page = services.market.coverage(limit=10, offset=0)
    assert page.total == 3
    up = next(c for c in page.items if c.ticker == "UP.US")
    assert up.interval == "1d"
    assert up.first_bar.date() == date(2025, 10, 1)
    assert up.last_bar.date() == date(2026, 4, 1)
    assert up.rows > 100
    only = services.market.coverage(ticker="DOWN.US", limit=10, offset=0)
    assert [c.ticker for c in only.items] == ["DOWN.US"]


# ---- orders -----------------------------------------------------------------


def test_orders_and_fills_filters(services, seeded):
    orders = services.orders.orders(tick_id=seeded["tick_id"], limit=10, offset=0)
    assert orders.total == 1
    order = orders.items[0]
    assert order.ticker == "UP.US"
    assert order.status == "filled"
    assert services.orders.orders(ticker="NOPE", limit=10, offset=0).total == 0
    fills = services.orders.fills(tick_id=seeded["tick_id"], limit=10, offset=0)
    assert fills.total == 1
    assert fills.items[0].order_client_id == order.client_id


# ---- ticks ------------------------------------------------------------------


def test_tick_list_and_detail(services, seeded):
    page = services.ticks.list(services.bootstrap_scope(), limit=10, offset=0)
    assert page.total == 1
    detail = services.ticks.get(services.bootstrap_scope(), seeded["tick_id"])
    assert detail.status == "ok"
    assert detail.summary["winner_strategy_id"] == seeded["active_id"]
    assert len(detail.orders) == 1


def test_tick_unknown_is_not_found(services):
    with pytest.raises(NotFoundError):
        services.ticks.get(services.bootstrap_scope(), "tick_nope")


def test_tick_dry_run(services):
    result = services.ticks.run(
        TickRequest(dry_run=True, as_of=date(2026, 3, 25), tickers=["UP.US"])
    )
    assert result.dry_run is True
    assert result.status in ("ok", "noop", "partial")
    assert services.ticks.list(services.bootstrap_scope(), limit=10, offset=0).total == 2


def test_tick_requires_universe(services):
    with pytest.raises(ValidationError):
        services.ticks.run(TickRequest(dry_run=True))


def test_tick_job(services):
    job = services.ticks.submit(TickRequest(dry_run=True, tickers=["UP.US"], as_of="2026-03-26"))
    done = services.jobs.wait(job.id, timeout=30)
    assert done.status == "succeeded", done.error
    assert done.result["dry_run"] is True


# ---- ingest -----------------------------------------------------------------


def test_ingest_runs_listing_and_trigger(services):
    job = services.ingest.submit(IngestRequest(kind="prices", tickers=["NEW.US"]))
    done = services.jobs.wait(job.id, timeout=30)
    assert done.status == "succeeded", done.error
    assert done.result["status"] == "ok"
    runs = services.ingest.runs(limit=10, offset=0)
    assert runs.total == 1
    assert runs.items[0].kind == "prices"
    assert runs.items[0].tickers_ok == 1
    series = services.market.bars("NEW.US", interval="1d")
    assert len(series.bars) == 3


def test_ingest_requires_tickers_or_exchange():
    with pytest.raises(ValueError):
        IngestRequest(kind="prices")


def test_ingest_without_configured_source_fails_fast(settings, seeded):
    from stonks.app.context import AppContext
    from stonks.app.services import Services

    svc = Services.create(AppContext(settings))  # no key, no fake source
    svc.start()
    try:
        with pytest.raises(ConfigurationError):
            svc.ingest.submit(IngestRequest(kind="prices", tickers=["X.US"]))
    finally:
        svc.shutdown()


# ---- lab --------------------------------------------------------------------


def _bah_request(**overrides) -> BacktestRequest:
    base = {
        "strategy": StrategyRef(
            class_path="stonks.strategies.examples.buy_and_hold:BuyAndHold",
            params={"ticker": "UP.US", "allocation": 1.0},
        ),
        "universe": ["UP.US"],
        "start": date(2025, 10, 1),
        "end": date(2026, 4, 1),
    }
    base.update(overrides)
    return BacktestRequest(**base)


def test_backtest_returns_plain_report(services):
    result = services.lab.run_backtest(_bah_request())
    assert result.final_return > 0.5
    assert result.interval == "1d"
    assert len(result.equity) > 100
    assert result.equity[0].value == pytest.approx(10_000.0)


def test_backtest_result_carries_its_benchmark(services):
    auto = services.lab.run_backtest(_bah_request())
    assert auto.benchmark is not None
    assert auto.benchmark.name == "EW"  # "auto": no SPY.US in the lake
    assert auto.benchmark.spec == "auto"
    assert auto.benchmark.members == ["UP.US"]
    assert len(auto.benchmark_equity) == len(auto.equity)
    assert auto.benchmark_equity[0].value == pytest.approx(auto.equity[0].value)

    vs_down = services.lab.run_backtest(_bah_request(benchmark="DOWN.US"))
    assert vs_down.benchmark.name == "DOWN.US"
    assert vs_down.benchmark.excess_cagr > 0

    off = services.lab.run_backtest(_bah_request(benchmark="none"))
    assert off.benchmark is None and off.benchmark_equity == []


def test_backtest_benchmark_default_comes_from_lab_config(services):
    services.lab._ctx.settings.lab.benchmark = "FLAT.US"
    assert services.lab.run_backtest(_bah_request()).benchmark.name == "FLAT.US"


def test_backtest_from_registered_strategy(services, seeded):
    result = services.lab.run_backtest(
        _bah_request(strategy=StrategyRef(strategy_id=seeded["active_id"]))
    )
    assert result.final_return > 0


def test_backtest_rejects_inverted_window():
    with pytest.raises(ValueError):
        _bah_request(start=date(2026, 4, 1), end=date(2025, 1, 1))


def test_backtest_job_is_json_safe(services):
    job = services.lab.submit_backtest(_bah_request())
    done = services.jobs.wait(job.id, timeout=60)
    assert done.status == "succeeded", done.error
    assert done.result["final_return"] > 0.5
    assert done.kind == "backtest"


def test_backtest_job_with_bad_strategy_is_rejected_before_queueing(services):
    with pytest.raises(ValidationError):
        services.lab.submit_backtest(_bah_request(strategy=StrategyRef(class_path="x:Y")))
    assert services.jobs.list(limit=10, offset=0).total == 0


def test_lab_run_job(services):
    req = LabRunRequest(
        strategy=StrategyRef(class_path="stonks.strategies.examples.momentum:Momentum", params={}),
        universe=["UP.US", "DOWN.US"],
        start=date(2025, 10, 1),
        end=date(2026, 4, 1),
        tuner="random",
        budget=2,
        survival_tests=["oos"],
        register_strategy=True,
    )
    job = services.lab.submit_lab_run(req)
    done = services.jobs.wait(job.id, timeout=120)
    assert done.status == "succeeded", done.error
    assert done.result["verdict"] in ("pass", "fail")
    assert [r["test_id"] for r in done.result["survival_reports"]] == ["oos"]
    sid = done.result["registered_strategy_id"]
    assert services.strategies.get(sid).status == "shadow"


def test_services_scrub_configured_secrets_from_job_errors(settings, seeded):
    from stonks.app.context import AppContext
    from stonks.app.services import Services

    settings.sources.eodhd.api_key = "vendor-key-xyz"
    svc = Services.create(AppContext(settings))
    assert "vendor-key-xyz" in list(svc.runner.secrets())
    from tests.integration.app.conftest import API_TOKEN

    assert API_TOKEN in list(svc.runner.secrets())


def _momentum_lab_request(**overrides) -> LabRunRequest:
    base = {
        "strategy": StrategyRef(class_path="stonks.strategies.examples.momentum:Momentum"),
        "universe": ["UP.US", "DOWN.US"],
        "start": date(2025, 10, 1),
        "end": date(2026, 4, 1),
        "tuner": "random",
        "budget": 2,
        "survival_tests": ["oos"],
    }
    base.update(overrides)
    return LabRunRequest(**base)


def test_lab_run_stops_at_first_trial_once_cancelled(services, capsys):
    from stonks.app.jobs import JobCancelled, JobContext

    ctx = JobContext(job_id="job_none", _store=services.runner.store)
    ctx.request_cancel()
    with pytest.raises(JobCancelled):
        services.lab.run_lab(_momentum_lab_request(budget=500), progress=ctx)
    # tuners swallow per-trial Exceptions; cancellation must not be one of them
    assert "trial.failed" not in capsys.readouterr().out


def test_running_lab_run_job_can_be_cancelled(services):
    job = services.lab.submit_lab_run(_momentum_lab_request(budget=1000))
    deadline = time.monotonic() + 30
    while services.jobs.get(job.id).status == "queued" and time.monotonic() < deadline:
        time.sleep(0.01)
    services.jobs.cancel(job.id)
    done = services.jobs.wait(job.id, timeout=30)
    assert done.status == "cancelled"


def test_finished_tick_job_cannot_be_cancelled(services):
    from stonks.app.errors import ConflictError

    job = services.ticks.submit(TickRequest(dry_run=True, tickers=["UP.US"]))
    services.jobs.wait(job.id, timeout=30)
    with pytest.raises(ConflictError):
        services.jobs.cancel(job.id)


def test_services_scrub_broker_keys_and_webhook_url(settings, seeded):
    from pydantic import SecretStr

    from stonks.app.context import AppContext
    from stonks.app.services import Services

    settings.brokers.alpaca.api_key = SecretStr("alpaca-key-1")
    settings.brokers.alpaca.secret_key = SecretStr("alpaca-secret-2")
    settings.notify.webhook.url = "https://hooks.example/T0/B0/secret"
    secrets = list(Services.create(AppContext(settings)).runner.secrets())
    assert {"alpaca-key-1", "alpaca-secret-2", "https://hooks.example/T0/B0/secret"} <= set(secrets)


# ---- catalog: caching, extra sources, registered strategies ------------------


def test_catalog_discovers_each_source_once_until_refreshed():
    calls = []

    class CountingSource:
        name = "counting"

        def discover(self):
            from stonks.strategies.examples.momentum import Momentum

            calls.append(1)
            return [Momentum]

    catalog = CatalogService(sources=[CountingSource()])
    catalog.strategies()
    catalog.strategies()
    catalog.strategy_class("stonks.strategies.examples.momentum:Momentum")
    assert len(calls) == 1
    catalog.refresh()
    catalog.strategies()
    assert len(calls) == 2


def test_catalog_add_source_invalidates_cache():
    from stonks.app.catalog import ClassListStrategySource

    catalog = CatalogService(sources=[])
    assert catalog.strategies() == []
    catalog.add_source(
        ClassListStrategySource("extra", ["stonks.strategies.examples.momentum:Momentum"])
    )
    assert [c.source for c in catalog.strategies()] == ["extra"]


def test_class_list_source_skips_modules_that_do_not_exist_yet():
    from stonks.app.catalog import ClassListStrategySource

    src = ClassListStrategySource(
        "studio",
        [
            "stonks.strategies.not_built_yet:RuleStrategy",
            "stonks.strategies.macro_regime:MacroRegimeFilter",
        ],
    )
    assert [c.__name__ for c in src.discover()] == ["MacroRegimeFilter"]


def test_default_catalog_offers_macro_regime_quality_value_and_rule_strategy():
    from stonks.app.services import default_strategy_sources

    catalog = CatalogService(sources=default_strategy_sources())
    paths = {c.class_path for c in catalog.strategies()}
    assert "stonks.strategies.macro_regime:MacroRegimeFilter" in paths
    assert "stonks.strategies.examples.quality_value:QualityValue" in paths
    assert "stonks.strategies.rule_based:RuleStrategy" in paths


def test_registered_strategy_class_resolves_outside_the_catalog(settings, seeded):
    from stonks.app.context import AppContext
    from stonks.app.services import Services

    svc = Services.create(AppContext(settings), strategy_sources=[])
    cls = svc.strategies.strategy_class(StrategyRef(strategy_id=seeded["active_id"]))
    assert cls.__name__ == "BuyAndHold"
    with pytest.raises(NotFoundError):
        svc.strategies.strategy_class(StrategyRef(strategy_id="nope"))


def test_lab_run_on_registered_strategy_outside_catalog_is_accepted(settings, seeded):
    from stonks.app.context import AppContext
    from stonks.app.services import Services

    svc = Services.create(AppContext(settings), strategy_sources=[])
    svc.start()
    try:
        job = svc.lab.submit_lab_run(
            LabRunRequest(
                strategy=StrategyRef(strategy_id=seeded["active_id"]),
                universe=["UP.US"],
                start=date(2025, 10, 1),
                end=date(2026, 4, 1),
                budget=1,
                survival_tests=["oos"],
            )
        )
        assert svc.jobs.wait(job.id, timeout=60).status == "succeeded"
    finally:
        svc.shutdown()


# ---- data sources -------------------------------------------------------------


def test_ingest_source_literal_matches_source_registry():
    from typing import get_args

    from stonks.app.ingest import SourceId
    from stonks.ingest.sources.registry import SOURCE_IDS

    assert get_args(SourceId) == SOURCE_IDS


def test_ingest_request_source_defaults_and_validates():
    assert IngestRequest(kind="prices", tickers=["A.US"]).source == "eodhd"
    assert IngestRequest(kind="prices", tickers=["A.US"], source="yahoo").source == "yahoo"
    with pytest.raises(ValueError):
        IngestRequest(kind="prices", tickers=["A.US"], source="bogus")


def test_context_builds_sources_through_the_registry(settings):
    from stonks.app.context import AppContext
    from stonks.ingest.sources.yahoo import YahooDataSource

    ctx = AppContext(settings)
    assert isinstance(ctx.build_source("yahoo"), YahooDataSource)
    with pytest.raises(ConfigurationError, match="EODHD_API_KEY"):
        ctx.build_source("eodhd")
    with pytest.raises(ConfigurationError):
        ctx.build_source()  # default source is eodhd
    with pytest.raises(ValidationError):
        ctx.build_source("bogus")


def test_ingest_submit_uses_the_requested_source(settings, seeded):
    from stonks.app.context import AppContext
    from stonks.app.services import Services
    from tests.integration.app.conftest import FakeDataSource

    svc = Services.create(AppContext(settings))  # no EODHD key
    svc.start()
    built: list[str | None] = []

    def spy(source_id=None):
        built.append(source_id)
        return FakeDataSource()  # hermetic: never reach the real vendor

    svc.context.build_source = spy  # type: ignore[method-assign]
    try:
        job = svc.ingest.submit(IngestRequest(kind="prices", tickers=["X.US"], source="yahoo"))
        assert svc.jobs.wait(job.id, timeout=30).status == "succeeded"
    finally:
        svc.shutdown()
    assert built and set(built) == {"yahoo"}


def test_list_sources_reports_configuration(settings, seeded):
    from stonks.app.context import AppContext
    from stonks.app.services import Services

    svc = Services.create(AppContext(settings))
    by_id = {s.id: s for s in svc.ingest.sources()}
    assert set(by_id) == {"eodhd", "yahoo", "defillama"}
    assert by_id["defillama"].configured is True  # no key needed
    assert by_id["eodhd"].default is True
    assert by_id["eodhd"].configured is False
    assert "EODHD_API_KEY" in (by_id["eodhd"].detail or "")
    assert by_id["yahoo"].configured is True

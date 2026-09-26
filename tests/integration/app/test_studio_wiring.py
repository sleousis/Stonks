"""Strategy Studio wired into the service container: catalog, startup job
registration, loading registered code strategies, rule asset classes, and
lab runs honouring ``[backtest.costs]``."""

from __future__ import annotations

import sys
from datetime import date

import pytest

from stonks.app.context import AppContext
from stonks.app.lab import BacktestRequest
from stonks.app.services import Services, default_strategy_sources
from stonks.app.strategies import StrategyRef
from stonks.app.studio import (
    STUDIO_BACKTEST_JOB,
    STUDIO_LAB_RUN_JOB,
    USER_MODULE_PREFIX,
    DraftCreate,
    StudioService,
    ValidateRequest,
)
from stonks.strategies.rule_based import RULE_STRATEGY_CLASS_PATH
from tests.integration.app.test_studio import GOOD_CODE, TREND


@pytest.fixture(autouse=True)
def _forget_user_modules():
    yield
    for name in [m for m in sys.modules if m.startswith(USER_MODULE_PREFIX)]:
        del sys.modules[name]


@pytest.fixture
def svc(settings, seeded, fake_source):
    services = Services.create(AppContext(settings, source_factory=lambda: fake_source))
    services.start()
    yield services
    services.shutdown()


def test_default_catalog_resolves_rule_strategy(svc):
    assert svc.catalog.strategy_class(RULE_STRATEGY_CLASS_PATH).__name__ == "RuleStrategy"
    ref = StrategyRef(class_path=RULE_STRATEGY_CLASS_PATH, params={"spec": TREND})
    job = svc.lab.submit_backtest(
        BacktestRequest(
            strategy=ref,
            universe=["UP.US", "DOWN.US"],
            start=date(2025, 11, 1),
            end=date(2026, 4, 1),
        )
    )
    assert svc.jobs.wait(job.id, timeout=60).status == "succeeded"


def test_app_catalog_and_lab_catalog_share_one_list():
    from stonks.app.catalog import CatalogService, class_path_of
    from stonks.lab.catalog import strategy_catalog

    app_paths = {c.class_path for c in CatalogService(default_strategy_sources()).strategies()}
    lab_paths = {class_path_of(cls) for cls in strategy_catalog().values()}
    assert app_paths == lab_paths | {RULE_STRATEGY_CLASS_PATH}


def test_studio_is_wired_at_startup(settings, seeded):
    svc = Services.create(AppContext(settings))
    assert isinstance(svc.studio, StudioService)
    assert {STUDIO_BACKTEST_JOB, STUDIO_LAB_RUN_JOB} <= set(svc.runner.kinds)


def test_rule_strategy_summary_uses_the_spec_asset_classes(svc):
    spec = {**TREND, "universe": {"asset_classes": ["crypto"]}}
    draft = svc.studio.create_draft(DraftCreate(name="crypto trend", spec=spec))
    sid = svc.studio.register_draft(draft.id).registered_strategy_id
    assert svc.strategies.get(sid).applicable_asset_classes == ["crypto"]


def _register_code_strategy(settings) -> str:
    first = Services.create(AppContext(settings))
    first.start()
    try:
        draft = first.studio.create_draft(
            DraftCreate(name="Up", kind="code", source_code=GOOD_CODE, spec={"edge": 0.02})
        )
        assert first.studio.validate_draft(draft.id, ValidateRequest()).valid
        return first.studio.register_draft(draft.id).registered_strategy_id
    finally:
        first.shutdown()
        for name in [m for m in sys.modules if m.startswith(USER_MODULE_PREFIX)]:
            del sys.modules[name]


def test_registered_code_strategy_loads_in_a_fresh_process(settings, seeded):
    settings.api.allow_code_strategies = True
    sid = _register_code_strategy(settings)

    fresh = Services.create(AppContext(settings))
    fresh.start()
    try:
        # the registry (and so the Ranker) imports it by class path
        with fresh.context.registry() as registry:
            strategy = registry.load(sid)
        assert type(strategy).__name__ == "AlwaysUp"
        assert strategy.estimate_return("UP.US", date(2026, 1, 5), None) == 0.02
    finally:
        fresh.shutdown()
    from stonks.app.user_strategies import UserStrategyFinder

    assert not any(isinstance(f, UserStrategyFinder) for f in sys.meta_path)


def test_registered_code_strategy_stays_unloadable_when_code_is_disabled(settings, seeded):
    settings.api.allow_code_strategies = True
    sid = _register_code_strategy(settings)
    settings.api.allow_code_strategies = False

    fresh = Services.create(AppContext(settings))
    fresh.start()
    try:
        with fresh.context.registry() as registry, pytest.raises(ImportError):
            registry.load(sid)
    finally:
        fresh.shutdown()


def test_lab_backtests_honour_configured_costs(svc, settings):
    from stonks.backtest.costs import CostModelSettings

    request = BacktestRequest(
        strategy=StrategyRef(
            class_path="stonks.strategies.examples.buy_and_hold:BuyAndHold",
            params={"ticker": "UP.US", "allocation": 1.0},
        ),
        universe=["UP.US"],
        start=date(2025, 10, 1),
        end=date(2026, 4, 1),
    )
    free = svc.lab.run_backtest(request).final_return
    settings.backtest.costs = CostModelSettings.realistic()
    costly = svc.lab.run_backtest(request).final_return
    assert costly < free
    # an explicit preset still wins
    zero = svc.lab.run_backtest(request.model_copy(update={"cost_model": "zero"})).final_return
    assert zero == pytest.approx(free)


def test_lab_runs_honour_configured_costs(svc, settings, monkeypatch):
    from stonks.app.lab import LabRunRequest
    from stonks.backtest.costs import CostModelSettings
    from stonks.lab import dataset as dataset_module

    seen: list[object] = []
    original = dataset_module.LabDataset.__init__

    def spy(self, *args, **kwargs):
        original(self, *args, **kwargs)
        seen.append(self.costs)

    monkeypatch.setattr(dataset_module.LabDataset, "__init__", spy)
    settings.backtest.costs = CostModelSettings.realistic()
    svc.lab.run_lab(
        LabRunRequest(
            strategy=StrategyRef(class_path="stonks.strategies.examples.momentum:Momentum"),
            universe=["UP.US"],
            start=date(2025, 10, 1),
            end=date(2026, 4, 1),
            budget=1,
            survival_tests=["oos"],
        )
    )
    assert seen and seen[0] == CostModelSettings.realistic()

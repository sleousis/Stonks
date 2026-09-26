"""LabService: registry-built suites, register-only-if-passes and the lab
cost-model option (BL-10, roadmap 11.4 / 11.6)."""

from __future__ import annotations

from datetime import date

import pytest

import stonks.app.lab as lab_module
from stonks.app.lab import LabRunRequest
from stonks.app.strategies import StrategyRef
from stonks.backtest.costs import CostModelSettings
from stonks.core.protocols import SurvivalReport


class _FixedTest:
    def __init__(self, test_id: str, passed: bool) -> None:
        self.id = test_id
        self._passed = passed

    def run(self, strategy, context) -> SurvivalReport:
        return SurvivalReport(test_id=self.id, passed=self._passed, metrics={})


def _request(**extra) -> LabRunRequest:
    base = {
        "strategy": StrategyRef(class_path="stonks.strategies.examples.momentum:Momentum"),
        "universe": ["UP.US", "DOWN.US"],
        "start": date(2025, 10, 1),
        "end": date(2026, 4, 1),
        "budget": 1,
        "survival_tests": ["oos"],
    }
    base.update(extra)
    return LabRunRequest(**base)


@pytest.fixture
def verdict(monkeypatch):
    """Make every survival test pass or fail on demand."""
    outcome = {"passed": True}

    def build(name, options=None, **_):
        return _FixedTest(name, outcome["passed"])

    monkeypatch.setattr(lab_module, "build_survival_test", build)
    return outcome


def test_register_if_passes_skips_a_failed_verdict(services, verdict):
    verdict["passed"] = False
    before = services.strategies.list(limit=100, offset=0).total
    view = services.lab.run_lab(_request(register_if_passes=True))
    assert view.verdict == "fail"
    assert view.registered_strategy_id is None
    assert services.strategies.list(limit=100, offset=0).total == before


def test_register_if_passes_registers_a_passing_verdict(services, verdict):
    view = services.lab.run_lab(_request(register_if_passes=True))
    assert view.verdict == "pass"
    assert services.strategies.get(view.registered_strategy_id).status == "shadow"


def test_register_strategy_always_registers_even_on_fail(services, verdict):
    verdict["passed"] = False
    view = services.lab.run_lab(_request(register_strategy=True))
    assert view.verdict == "fail"
    assert services.strategies.get(view.registered_strategy_id).status == "shadow"


def test_register_if_passes_skips_the_custom_register_hook_too(services, verdict):
    verdict["passed"] = False
    calls = []
    cls = services.strategies.strategy_class(_request().strategy)
    view = services.lab.run_lab_class(
        cls,
        _request(register_if_passes=True),
        register=lambda s, r: calls.append(s) or "x",
    )
    assert calls == [] and view.registered_strategy_id is None


def test_suite_is_built_from_the_registry_in_resolved_order(services, monkeypatch):
    built: list[tuple[str, object]] = []

    def build(name, options=None, **_):
        built.append((name, options))
        return _FixedTest(name, True)

    monkeypatch.setattr(lab_module, "build_survival_test", build)
    view = services.lab.run_lab(
        _request(
            survival_tests=["permutation", "walk_forward", "oos"],
            mcpt={"n_permutations": 3},
            walk_forward={"n_splits": 2},
        )
    )
    assert [n for n, _ in built] == ["mcpt", "walk_forward", "oos"]
    assert built[0][1]["n_permutations"] == 3
    assert built[1][1]["config"].n_splits == 2
    assert built[2][1] is None
    assert [r.test_id for r in view.survival_reports] == ["mcpt", "walk_forward", "oos"]


def test_walk_forward_defaults_come_from_settings(services, monkeypatch):
    built: dict[str, object] = {}

    def build(name, options=None, **_):
        built[name] = options
        return _FixedTest(name, True)

    monkeypatch.setattr(lab_module, "build_survival_test", build)
    services.lab.run_lab(_request(survival_tests=["walk_forward"]))
    assert built["walk_forward"]["config"] == services.lab._ctx.settings.lab.walk_forward


def _capture_dataset_costs(monkeypatch) -> list:
    seen: list = []
    real = lab_module.LabDataset

    def spy(**kwargs):
        seen.append(kwargs.get("costs"))
        return real(**kwargs)

    monkeypatch.setattr(lab_module, "LabDataset", spy)
    return seen


def test_lab_cost_model_preset_reaches_the_dataset(services, verdict, monkeypatch):
    seen = _capture_dataset_costs(monkeypatch)
    services.lab.run_lab(_request(cost_model="realistic"))
    assert seen == [CostModelSettings.realistic()]


def test_lab_cost_model_settings_reach_the_dataset(services, verdict, monkeypatch):
    seen = _capture_dataset_costs(monkeypatch)
    services.lab.run_lab(_request(cost_model={"impact_bps": 7.0}))
    assert seen == [CostModelSettings(impact_bps=7.0)]


def test_lab_without_cost_model_uses_configured_costs(services, verdict, monkeypatch):
    seen = _capture_dataset_costs(monkeypatch)
    services.lab.run_lab(_request())
    assert seen == [services.lab._ctx.settings.backtest.costs]

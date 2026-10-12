"""Plugins through ``stonks.*`` entry points: an outside package adds
strategies, objectives, tuners and survival tests. Hermetic: the entry
points come from a fake distribution, never the installed ones."""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass
from typing import Any

import pytest

from stonks.app.lab import LabRunOptions, build_tuner
from stonks.core.params import ParameterSpec
from stonks.core.protocols import SurvivalReport
from stonks.lab import plugins
from stonks.lab.objectives import SharpeObjective, build_objective, objective_names
from stonks.lab.parallel import ParallelSettings
from stonks.lab.tuning.random import RandomTuner
from stonks.strategies.base import BaseStrategy

_MODULE = "stonks_fake_plugin_pkg"


class PluginStrategy(BaseStrategy):
    """A strategy shipped by an outside package."""

    id = "fake_plugin_strategy"

    @classmethod
    def parameter_spec(cls):
        return [ParameterSpec(name="n", kind="int", default=3, bounds=(1, 9))]

    def estimate_return(self, ticker, as_of, lake):  # pragma: no cover - unused
        return None

    def decide(self, my_picks, portfolio, prices, as_of):  # pragma: no cover - unused
        return []


class PluginObjective(SharpeObjective):
    """Plugin Sharpe.

    Sharpe from an outside package."""


class PluginTuner(RandomTuner):
    """Plugin random.

    Random search from an outside package."""


class PluginSurvivalTest:
    """Always passes."""

    id = "fake_plugin_test"

    def run(self, strategy, dataset) -> SurvivalReport:  # pragma: no cover - unused
        return SurvivalReport(name=self.id, passed=True, metrics={})


class NotAStrategy:
    id = "not_a_strategy"


@dataclass
class _FakeDist:
    name: str = "stonks-fake-plugin"
    version: str = "0.1.0"


@dataclass
class _FakeEntryPoint:
    name: str
    value: str
    group: str
    dist: Any = None

    def load(self) -> Any:
        module_name, _, attr = self.value.partition(":")
        module = sys.modules[module_name]
        return getattr(module, attr) if attr else module


_ENTRY_POINTS = [
    ("stonks.strategies", "fake_plugin_strategy", "PluginStrategy"),
    ("stonks.strategies", "not_a_strategy", "NotAStrategy"),
    ("stonks.strategies", "broken", "Missing"),
    ("stonks.objectives", "plugin_sharpe", "PluginObjective"),
    ("stonks.objectives", "sharpe", "PluginObjective"),  # may not replace a built-in
    ("stonks.tuners", "plugin_random", "PluginTuner"),
    ("stonks.survival_tests", "fake_plugin_test", "PluginSurvivalTest"),
]


@pytest.fixture
def fake_plugins(monkeypatch):
    module = types.ModuleType(_MODULE)
    for cls in (PluginStrategy, PluginObjective, PluginTuner, PluginSurvivalTest, NotAStrategy):
        setattr(module, cls.__name__, cls)
    monkeypatch.setitem(sys.modules, _MODULE, module)
    dist = _FakeDist()

    def fake_entry_points(*, group: str) -> list[_FakeEntryPoint]:
        return [
            _FakeEntryPoint(name, f"{_MODULE}:{attr}", g, dist)
            for g, name, attr in _ENTRY_POINTS
            if g == group
        ]

    monkeypatch.setattr(plugins, "entry_points", fake_entry_points)
    from stonks.lab import objectives
    from stonks.lab.tuning import registry as tuners

    monkeypatch.setattr(objectives, "_PLAIN", dict(objectives._PLAIN))
    monkeypatch.setattr(objectives, "_CV", list(objectives._CV))
    monkeypatch.setattr(tuners, "_TUNERS", dict(tuners.tuner_classes()))
    plugins.clear_cache()
    yield
    plugins.clear_cache()


def test_the_groups_are_named():
    assert set(plugins.GROUPS) == {
        "stonks.strategies",
        "stonks.objectives",
        "stonks.tuners",
        "stonks.survival_tests",
    }


def test_a_plugin_strategy_resolves_through_the_lab_catalog(fake_plugins):
    from stonks.lab.catalog import load_strategy_class, resolve_strategy, strategy_catalog

    assert strategy_catalog()["fake_plugin_strategy"] is PluginStrategy
    assert resolve_strategy("fake_plugin_strategy") is PluginStrategy
    assert resolve_strategy("PluginStrategy") is PluginStrategy
    assert load_strategy_class(f"{PluginStrategy.__module__}:PluginStrategy") is PluginStrategy
    assert "not_a_strategy" not in strategy_catalog()


def test_the_cli_validates_a_plugin_strategy(fake_plugins):
    from stonks.cli import _validated_strategy

    cls, pinned = _validated_strategy("fake_plugin_strategy", '{"n": 4}')
    assert cls is PluginStrategy
    assert pinned == {"n": 4}


def test_a_plugin_objective_is_accepted(fake_plugins):
    assert "plugin_sharpe" in objective_names()
    assert LabRunOptions(objective="plugin_sharpe").objective == "plugin_sharpe"
    built = build_objective("plugin_sharpe")
    assert isinstance(built, PluginObjective)
    assert built.name == "plugin_sharpe"
    # the built-in keeps its name
    assert type(build_objective("sharpe")) is SharpeObjective


def test_a_plugin_tuner_is_accepted(fake_plugins):
    options = LabRunOptions(tuner="plugin_random")
    assert isinstance(build_tuner(options, ParallelSettings(max_workers=1)), PluginTuner)


def test_a_plugin_survival_test_is_registered(fake_plugins):
    from stonks.lab.survival import registry

    assert registry.survival_test_classes()["fake_plugin_test"] is PluginSurvivalTest
    assert "fake_plugin_test" in registry.survival_test_names()
    assert LabRunOptions(survival_tests=["fake_plugin_test"]).suite() == ["fake_plugin_test"]


def test_plugins_load_once_and_are_logged(fake_plugins, monkeypatch):
    calls: list[str] = []
    events: list[tuple[str, dict]] = []
    real = plugins.entry_points

    def counting(*, group: str):
        calls.append(group)
        return real(group=group)

    class _Log:
        def info(self, event: str, **kw: Any) -> None:
            events.append((event, kw))

        def warning(self, event: str, **kw: Any) -> None:
            events.append((event, kw))

    monkeypatch.setattr(plugins, "entry_points", counting)
    monkeypatch.setattr(plugins, "_log", _Log())
    plugins.clear_cache()
    first = plugins.plugin_objects("stonks.strategies")
    second = plugins.plugin_objects("stonks.strategies")
    assert first is second
    assert calls == ["stonks.strategies"]
    loaded = [kw for e, kw in events if e == "lab.plugin.loaded"]
    assert {kw["name"] for kw in loaded} == {"fake_plugin_strategy", "not_a_strategy"}
    assert all(kw["distribution"] == "stonks-fake-plugin" for kw in loaded)
    failed = [kw for e, kw in events if e == "lab.plugin.failed"]
    assert [kw["name"] for kw in failed] == ["broken"]


def test_no_plugins_means_nothing_changes(monkeypatch):
    monkeypatch.setattr(plugins, "entry_points", lambda *, group: [])
    plugins.clear_cache()
    try:
        from stonks.lab.catalog import strategy_catalog

        assert "fake_plugin_strategy" not in strategy_catalog()
        assert plugins.plugin_objects("stonks.objectives") == ()
    finally:
        plugins.clear_cache()

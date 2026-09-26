"""Unit tests for the survival-test registry and suite presets (BL-10)."""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

from stonks.lab.survival import registry
from stonks.lab.survival.oos import OutOfSampleTest
from stonks.lab.survival.permutation import MonteCarloPermutationTest
from stonks.lab.survival.walk_forward import WalkForwardConfig, WalkForwardTest


class _Recorder:
    def __init__(self) -> None:
        self.warnings: list[tuple[str, dict]] = []

    def warning(self, event: str, **kw) -> None:
        self.warnings.append((event, kw))

    def info(self, event: str, **kw) -> None:
        pass


def test_every_existing_test_is_discovered():
    names = registry.survival_test_names()
    for expected in (
        "oos",
        "period_stability",
        "perturbation",
        "drift",
        "runs_test",
        "mcpt",
        "walk_forward",
        "walk_forward_mcpt",
    ):
        assert expected in names
    assert names == sorted(names)


def test_build_default_instance():
    test = registry.build_survival_test("oos")
    assert isinstance(test, OutOfSampleTest)
    assert test.id == "oos"


def test_build_passes_options_as_kwargs():
    test = registry.build_survival_test("mcpt", {"n_permutations": 3, "retune": True})
    assert isinstance(test, MonteCarloPermutationTest)
    assert test._n == 3
    assert test._retune is True


def test_build_walk_forward_with_config_option():
    cfg = WalkForwardConfig(n_splits=2)
    test = registry.build_survival_test("walk_forward", {"config": cfg})
    assert isinstance(test, WalkForwardTest)
    assert test._cfg == cfg


def test_unknown_name_raises_and_lists_valid_ones():
    with pytest.raises(ValueError) as exc:
        registry.build_survival_test("nope")
    msg = str(exc.value)
    assert "nope" in msg
    assert "oos" in msg and "walk_forward" in msg


def test_bad_option_raises_value_error():
    with pytest.raises(ValueError, match="oos"):
        registry.build_survival_test("oos", {"not_a_param": 1})


def test_presets_exist_and_quick_is_oos_period_stability():
    assert {"quick", "standard", "promotion"} <= set(registry.SUITE_PRESETS)
    assert registry.resolve_preset("quick") == ["oos", "period_stability"]


def test_promotion_contains_walk_forward_and_permutation_test():
    names = registry.resolve_preset("promotion")
    assert "oos" in names and "walk_forward" in names and "mcpt" in names


def test_integration_2_preset_contents():
    assert registry.resolve_preset("quick") == ["oos", "period_stability"]
    assert registry.resolve_preset("standard") == [
        "oos",
        "period_stability",
        "perturbation",
        "walk_forward",
        "deflated_sharpe",
        "cost_stress",
        "signal_ic",
    ]
    promotion = registry.resolve_preset("promotion")
    assert "cross_instrument" in promotion
    for test_id in (
        "oos",
        "walk_forward",
        "deflated_sharpe",
        "pbo",
        "mc_trades",
        "cost_stress",
        "plateau",
        "benchmark_relative",
        "mcpt",
        "event_study",
        "vs_random",
    ):
        assert test_id in promotion
    # every preset id is registered: none is silently skipped
    known = set(registry.survival_test_names())
    for name, ids in registry.SUITE_PRESETS.items():
        assert set(ids) <= known, name


def test_preset_with_missing_id_warns_and_skips(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(registry, "_log", rec)
    monkeypatch.setitem(registry.SUITE_PRESETS, "tmp", ("oos", "not_landed_yet", "drift"))
    assert registry.resolve_preset("tmp") == ["oos", "drift"]
    assert rec.warnings and rec.warnings[0][1]["missing"] == ["not_landed_yet"]


def test_unknown_preset_raises_and_lists_presets():
    with pytest.raises(ValueError, match="promotion"):
        registry.resolve_preset("nope")


def test_new_test_module_in_a_package_is_discovered_without_editing_a_list(tmp_path: Path):
    pkg = tmp_path / "dummy_survival_pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "_helpers.py").write_text(
        "class Hidden:\n    id = 'hidden'\n    def run(self, s, c): ...\n"
    )
    (pkg / "coin_flip.py").write_text(
        textwrap.dedent(
            """
            from pydantic import BaseModel

            class CoinFlipTest:
                id = "coin_flip"

                class Options(BaseModel):
                    heads: bool = True

                def __init__(self, heads: bool = True) -> None:
                    self.heads = heads

                @classmethod
                def build(cls, options):
                    return cls(heads=options.heads)

                def run(self, strategy, context):
                    raise NotImplementedError

            class _Private:
                id = "private"
                def run(self, s, c): ...

            class NotATest:
                pass
            """
        )
    )
    sys.path.insert(0, str(tmp_path))
    try:
        import dummy_survival_pkg

        found = registry.discover_survival_tests(dummy_survival_pkg)
        assert set(found) == {"coin_flip"}
        test = registry.build_survival_test("coin_flip", {"heads": False}, tests=found)
        assert test.heads is False
        with pytest.raises(ValueError, match="coin_flip"):
            registry.build_survival_test("coin_flip", {"tails": 1}, tests=found)
    finally:
        sys.path.remove(str(tmp_path))
        sys.modules.pop("dummy_survival_pkg", None)
        sys.modules.pop("dummy_survival_pkg.coin_flip", None)
        sys.modules.pop("dummy_survival_pkg._helpers", None)


def test_resolve_suite_prefers_explicit_tests_over_preset():
    assert registry.resolve_suite(["drift"], preset="promotion") == ["drift"]
    assert registry.resolve_suite(None, preset="quick") == ["oos", "period_stability"]
    assert registry.resolve_suite(None, preset=None, default="quick") == ["oos", "period_stability"]


def test_preset_options_are_valid_for_their_tests():
    assert registry.preset_options("promotion") == {
        "mcpt": {"n_permutations": 200, "retune": "auto"},
        "cross_instrument": {"held_out_auto": 3},
    }
    assert registry.preset_options("quick") == {}
    for name in registry.preset_names():
        for test_id, options in registry.preset_options(name).items():
            assert test_id in registry.SUITE_PRESETS[name]
            registry.build_survival_test(test_id, options)
    # callers get a copy, never the table itself
    registry.preset_options("promotion")["mcpt"]["retune"] = False
    assert registry.preset_options("promotion")["mcpt"]["retune"] == "auto"

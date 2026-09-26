"""Lab request models: survival tests from the registry, presets,
registration modes and the cost-model option (BL-10, roadmap 11.4 / 11.6)."""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from stonks.app.lab import BacktestRequest, LabRunRequest
from stonks.app.strategies import StrategyRef
from stonks.backtest.costs import CostModelSettings
from stonks.lab.survival.registry import resolve_preset


def _req(**extra) -> LabRunRequest:
    return LabRunRequest(
        strategy=StrategyRef(class_path="stonks.strategies.examples.momentum:Momentum"),
        universe=["A.US"],
        start=date(2025, 1, 1),
        end=date(2025, 6, 1),
        **extra,
    )


def test_default_suite_is_the_quick_preset():
    assert _req().suite() == ["oos", "period_stability"]


def test_registering_defaults_to_the_promotion_preset():
    promotion = resolve_preset("promotion")
    assert _req(register_strategy=True).suite() == promotion
    assert _req(register_if_passes=True).suite() == promotion
    assert "walk_forward" in promotion


def test_preset_is_used_when_no_tests_are_given():
    assert _req(preset="standard").suite() == resolve_preset("standard")


def test_explicit_tests_win_over_preset_and_registration_default():
    assert _req(survival_tests=["drift"], preset="promotion").suite() == ["drift"]
    assert _req(survival_tests=["oos"], register_strategy=True).suite() == ["oos"]


def test_legacy_permutation_name_maps_to_the_mcpt_test():
    assert _req(survival_tests=["permutation", "oos"]).suite() == ["mcpt", "oos"]


def test_any_registered_test_is_accepted():
    assert _req(survival_tests=["walk_forward_mcpt"]).suite() == ["walk_forward_mcpt"]


def test_unknown_test_is_rejected_listing_the_valid_ones():
    with pytest.raises(ValidationError) as exc:
        _req(survival_tests=["nope"])
    assert "nope" in str(exc.value) and "period_stability" in str(exc.value)


def test_unknown_preset_is_rejected():
    with pytest.raises(ValidationError, match="promotion"):
        _req(preset="nope")


def test_empty_test_list_is_rejected():
    with pytest.raises(ValidationError):
        _req(survival_tests=[])


def test_options_need_their_test_in_the_resolved_suite():
    with pytest.raises(ValidationError, match="walk_forward"):
        _req(walk_forward={"n_splits": 2})  # quick has no walk_forward
    assert _req(walk_forward={"n_splits": 2}, register_strategy=True).walk_forward is not None
    with pytest.raises(ValidationError, match="permutation"):
        _req(survival_tests=["oos"], mcpt={"n_permutations": 2})
    assert _req(survival_tests=["mcpt"], mcpt={"n_permutations": 2}).mcpt is not None
    assert _req(survival_tests=["permutation"], mcpt={"n_permutations": 2}).mcpt is not None


def test_register_modes_are_exclusive():
    with pytest.raises(ValidationError, match="register_if_passes"):
        _req(register_strategy=True, register_if_passes=True)


def test_lab_cost_model_accepts_a_preset_name_or_settings():
    assert _req().cost_model is None
    assert _req(cost_model="realistic").cost_model == "realistic"
    custom = _req(cost_model={"impact_bps": 25.0}).cost_model
    assert isinstance(custom, CostModelSettings)
    assert custom.impact_bps == 25.0
    with pytest.raises(ValidationError):
        _req(cost_model="expensive")


def test_backtest_cost_model_accepts_settings_too():
    req = BacktestRequest(
        strategy=StrategyRef(class_path="stonks.strategies.examples.momentum:Momentum"),
        universe=["A.US"],
        start=date(2025, 1, 1),
        end=date(2025, 6, 1),
        cost_model={"default": {"fee_flat": 1.0}},
    )
    assert isinstance(req.cost_model, CostModelSettings)
    with pytest.raises(ValidationError):
        BacktestRequest(
            strategy=StrategyRef(class_path="stonks.strategies.examples.momentum:Momentum"),
            universe=["A.US"],
            start=date(2025, 1, 1),
            end=date(2025, 6, 1),
            cost_model={"impact_bps": 1.0},
            fee_per_trade=1.0,
        )

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


# ---- survival-test options (Integration 2) --------------------------------------


def test_test_options_are_validated_by_each_tests_options_model():
    req = _req(survival_tests=["oos"], test_options={"oos": {"mode": "sharpe", "min_trades": 0}})
    assert req.test_options == {"oos": {"mode": "sharpe", "min_trades": 0}}
    assert req.survival_options("oos") == {"mode": "sharpe", "min_trades": 0}
    assert req.survival_options("period_stability") is None


def test_test_options_for_an_unknown_test_are_rejected_listing_the_valid_ones():
    with pytest.raises(ValidationError) as exc:
        _req(survival_tests=["oos"], test_options={"bogus": {"x": 1}})
    assert "bogus" in str(exc.value) and "period_stability" in str(exc.value)


def test_unknown_or_invalid_option_names_are_rejected():
    with pytest.raises(ValidationError, match="nope"):
        _req(survival_tests=["oos"], test_options={"oos": {"nope": 1}})
    with pytest.raises(ValidationError, match="min_trades"):
        _req(survival_tests=["oos"], test_options={"oos": {"min_trades": -1}})
    with pytest.raises(ValidationError, match="max_sharpe_stdev"):
        _req(test_options={"period_stability": {"max_sharpe_stdev": 2.0}})


@pytest.mark.parametrize(
    ("test_id", "options", "needle"),
    [
        ("walk_forward", {"config": {"n_splits": 2}}, "walk_forward field"),
        ("mcpt", {"tuning": {}}, "tuning"),
    ],
)
def test_object_valued_constructor_arguments_are_not_options(test_id, options, needle):
    with pytest.raises(ValidationError, match=needle):
        _req(survival_tests=[test_id], test_options={test_id: options})


def test_test_options_need_their_test_in_the_resolved_suite():
    with pytest.raises(ValidationError, match="pbo"):
        _req(survival_tests=["oos"], test_options={"pbo": {"max_pbo": 0.3}})
    ok = _req(preset="promotion", test_options={"pbo": {"max_pbo": 0.3}})
    assert ok.survival_options("pbo") == {"max_pbo": 0.3}


def test_test_options_accept_the_legacy_permutation_name():
    req = _req(survival_tests=["permutation"], test_options={"permutation": {"n_permutations": 3}})
    assert req.survival_options("mcpt") == {"n_permutations": 3}


@pytest.mark.parametrize(
    ("test_id", "options"),
    [
        ("deflated_sharpe", {"min_dsr": 0.9, "include_prior_runs": False}),
        ("pbo", {"n_blocks": 8, "max_pbo": 0.3}),
        ("mc_trades", {"n_paths": 1000, "min_trades": 5}),
        ("cost_stress", {"stress_multiplier": 3.0, "max_cost_sharpe": 0.2}),
        ("plateau", {"step": 0.1, "min_neighbours": 1}),
        ("cross_instrument", {"held_out": ["B.US"], "min_tickers": 2}),
    ],
)
def test_wave2_tests_take_options(test_id, options):
    req = _req(survival_tests=[test_id], test_options={test_id: options})
    assert req.survival_options(test_id) == options


def test_mcpt_field_and_test_options_merge_with_test_options_winning():
    req = _req(
        survival_tests=["mcpt"],
        mcpt={"n_permutations": 2, "max_p_value": 0.1},
        test_options={"mcpt": {"n_permutations": 4}},
    )
    merged = req.survival_options("mcpt")
    assert merged["n_permutations"] == 4 and merged["max_p_value"] == 0.1


def test_benchmark_option_defaults_to_config():
    assert _req().benchmark is None
    assert _req(benchmark="QQQ.US").benchmark == "QQQ.US"
    assert _req(benchmark="none").benchmark == "none"


# ---- W2.4: embargo, walk-forward fields, scoring windows ------------------------


def test_embargo_bars_defaults_to_config_and_is_checked_against_the_window():
    assert _req().embargo_bars is None
    assert _req(embargo_bars=5).embargo_bars == 5
    with pytest.raises(ValidationError, match="embargo"):
        _req(embargo_bars=500)  # no validation window left in five months
    with pytest.raises(ValidationError):
        _req(embargo_bars=-1)


def test_walk_forward_takes_the_new_fields():
    req = _req(
        survival_tests=["walk_forward"],
        walk_forward={"min_wfe": None, "matrix": True, "matrix_min_pass_share": 0.5},
    )
    assert req.walk_forward.min_wfe is None and req.walk_forward.matrix is True


@pytest.mark.parametrize("test_id", ["perturbation", "runs_test", "period_stability"])
def test_scoring_window_options(test_id):
    req = _req(survival_tests=[test_id], test_options={test_id: {"window": "full"}})
    assert req.survival_options(test_id) == {"window": "full"}
    with pytest.raises(ValidationError, match="window"):
        _req(survival_tests=[test_id], test_options={test_id: {"window": "train"}})


# ---- preset options: the promotion preset's MCPT --------------------------------


def test_mcpt_options_default_to_200_permutations_and_accept_auto_retune():
    from stonks.app.lab import McptOptions

    assert McptOptions().n_permutations == 200
    assert McptOptions(retune="auto").retune == "auto"
    with pytest.raises(ValidationError):
        McptOptions(retune="sometimes")


def test_promotion_preset_gives_mcpt_200_permutations_and_auto_retune():
    expected = {"n_permutations": 200, "retune": "auto"}
    assert _req(register_strategy=True).survival_options("mcpt") == expected
    assert _req(preset="promotion").survival_options("mcpt") == expected
    # explicit test lists carry no preset options
    assert _req(survival_tests=["mcpt"]).survival_options("mcpt") is None


def test_request_options_override_preset_options():
    req = _req(preset="promotion", mcpt={"retune": False})
    assert req.survival_options("mcpt") == {"n_permutations": 200, "retune": False}
    req = _req(preset="promotion", test_options={"mcpt": {"n_permutations": 20}})
    assert req.survival_options("mcpt") == {"n_permutations": 20, "retune": "auto"}

"""Unit tests for the PBO survival test (BL-15, CSCV over the trial matrix)."""

from __future__ import annotations

import numpy as np
import pytest

from stonks.lab.survival import registry
from stonks.lab.survival.pbo import PBOTest
from stonks.lab.trials import LabRunContext, TrialMatrix, TrialRecord


def _noise(t: int, n: int, seed: int = 11) -> np.ndarray:
    return np.random.default_rng(seed).normal(0.0, 0.01, size=(t, n))


def _ctx(values: np.ndarray | None, n_run: int | None = None, ledger=None) -> LabRunContext:
    n = 0 if values is None else values.shape[1]
    n_run = n if n_run is None else n_run
    matrix = None if values is None else TrialMatrix(index=np.arange(len(values)), values=values)
    return LabRunContext(
        setup=None,  # type: ignore[arg-type]
        run_id="run-1",
        ledger=ledger,
        trials=[TrialRecord(i, {}, 0.0) for i in range(n_run)],
        trial_matrix=matrix,
        n_trials_run=n_run,
        n_trials_class=n_run,
    )


def _bound(ctx: LabRunContext, **options) -> PBOTest:
    test = PBOTest(**options)
    test.bind_run(ctx)
    return test


def test_defaults():
    test = PBOTest()
    assert test.id == "pbo"
    assert test.n_blocks == 10
    assert test.max_combinations == 5000
    assert test.max_pbo == 0.2
    assert test.min_trials == 8


def test_pure_noise_has_pbo_near_half_and_fails():
    out = _bound(_ctx(_noise(1000, 50))).evaluate()
    assert 0.3 <= out.metrics["pbo"] <= 0.7
    assert out.passed is False
    assert "PBO" in out.notes


def test_a_dominant_true_column_has_pbo_near_zero_and_passes():
    values = _noise(1000, 50)
    values[:, 0] += 0.004  # a real edge, visible in every block
    out = _bound(_ctx(values)).evaluate()
    assert out.metrics["pbo"] <= 0.05
    assert out.passed is True, out.notes


def test_sampled_combinations_are_reproducible():
    values = _noise(800, 20)
    a = _bound(_ctx(values), n_blocks=16, seed=5).evaluate().metrics
    b = _bound(_ctx(values), n_blocks=16, seed=5).evaluate().metrics
    assert a["n_combinations"] == 5000  # C(16, 8) = 12870 > 5000, so sampled
    assert a == b


def test_metrics_carry_the_inputs():
    m = _bound(_ctx(_noise(400, 12))).evaluate().metrics
    for key in (
        "pbo",
        "degradation_slope",
        "p_loss",
        "n_combinations",
        "n_trials",
        "n_trials_usable",
        "n_bars",
        "n_blocks",
    ):
        assert key in m, key
    assert m["n_trials"] == 12 and m["n_trials_usable"] == 12
    assert m["n_bars"] == 400 and m["n_blocks"] == 10
    assert m["n_combinations"] == 252  # C(10, 5)


def test_too_few_trials_fail_with_a_note():
    out = _bound(_ctx(_noise(400, 7))).evaluate()
    assert out.passed is False
    assert "insufficient data" in out.notes
    assert "7 usable trials < 8" in out.notes


def test_failed_trials_do_not_count_towards_the_minimum():
    values = _noise(400, 12)
    values[:, 5:] = np.nan
    out = _bound(_ctx(values)).evaluate()
    assert out.passed is False
    assert out.metrics["n_trials"] == 12
    assert out.metrics["n_trials_usable"] == 5
    assert "insufficient data" in out.notes


def test_too_few_bars_fail_with_a_note():
    out = _bound(_ctx(_noise(19, 20))).evaluate()  # 2 * 10 blocks = 20 needed
    assert out.passed is False
    assert "insufficient data" in out.notes
    assert "bars" in out.notes


def test_unbound_test_fails_with_insufficient_data():
    out = PBOTest().evaluate()
    assert out.passed is False
    assert "insufficient data" in out.notes


def test_missing_trial_matrix_fails_with_insufficient_data():
    out = _bound(_ctx(None, n_run=30)).evaluate()
    assert out.passed is False
    assert "insufficient data" in out.notes
    assert out.metrics["n_trials"] == 30


def test_falls_back_to_the_ledger_matrix():
    values = _noise(400, 12)

    class _Ledger:
        def trial_matrix(self, run_id):
            return TrialMatrix(index=np.arange(400), values=values)

    ctx = _ctx(values, ledger=_Ledger())
    ctx.trial_matrix = None
    assert _bound(ctx).evaluate().metrics == _bound(_ctx(values)).evaluate().metrics


def test_run_ignores_the_strategy_and_judges_the_bound_trials():
    out = _bound(_ctx(_noise(1000, 50))).run(strategy=None, context=None)
    assert out.test_id == "pbo"
    assert 0.3 <= out.metrics["pbo"] <= 0.7


def test_registry_builds_with_options_and_rejects_bad_ones():
    test = registry.build_survival_test("pbo", {"n_blocks": 8, "max_pbo": 0.1})
    assert isinstance(test, PBOTest)
    assert test.n_blocks == 8 and test.max_pbo == 0.1
    for bad in (
        {"n_blocks": 9},
        {"n_blocks": 6},
        {"n_blocks": 18},
        {"max_pbo": 1.5},
        {"max_combinations": 0},
        {"min_trials": 1},
    ):
        with pytest.raises(ValueError):
            registry.build_survival_test("pbo", bad)

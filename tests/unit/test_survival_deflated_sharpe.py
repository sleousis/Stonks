"""Unit tests for the deflated Sharpe survival test (BL-14).

``DeflatedSharpeTest.evaluate(val_returns)`` judges the selected strategy's
validation returns against the trials bound through ``bind_run``, so these
tests build ``LabRunContext`` objects and return series by hand.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from stonks.lab.survival import registry
from stonks.lab.survival.deflated_sharpe import DeflatedSharpeTest
from stonks.lab.trials import LabRunContext, TrialMatrix, TrialRecord
from stonks.stats.sharpe import psr, return_moments

PPY = 252.0


def _returns(sharpe_annual: float, n: int, sd: float = 0.01, seed: int = 1) -> np.ndarray:
    z = np.random.default_rng(seed).standard_normal(n)
    z = (z - z.mean()) / z.std(ddof=1)
    return sharpe_annual / math.sqrt(PPY) * sd + sd * z


def _noise(t: int, n: int, seed: int = 7) -> np.ndarray:
    return np.random.default_rng(seed).normal(0.0, 0.01, size=(t, n))


def _ctx(
    values: np.ndarray | None,
    n_run: int | None = None,
    n_class: int | None = None,
    ledger=None,
) -> LabRunContext:
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
        n_trials_class=n_run if n_class is None else n_class,
    )


def _bound(ctx: LabRunContext, **options) -> DeflatedSharpeTest:
    test = DeflatedSharpeTest(**options)
    test.bind_run(ctx)
    return test


def test_defaults():
    test = DeflatedSharpeTest()
    assert test.id == "deflated_sharpe"
    assert test.min_dsr == 0.95
    assert test.include_prior_runs is True
    assert test.n_eff_method == "effective_rank"


def test_strong_strategy_passes_and_noise_fails():
    ctx = _ctx(_noise(300, 20))
    strong = _bound(ctx).evaluate(_returns(4.0, 504))
    assert strong.passed is True, strong.notes
    noisy = _bound(ctx).evaluate(_returns(0.3, 504))
    assert noisy.passed is False
    assert noisy.metrics["dsr"] < 0.95


def test_more_noise_trials_lower_the_dsr():
    val = _returns(1.5, 504)
    few = _bound(_ctx(_noise(300, 5))).evaluate(val).metrics["dsr"]
    many = _bound(_ctx(_noise(300, 200))).evaluate(val).metrics["dsr"]
    assert many < few


def test_prior_runs_count_as_trials():
    val = _returns(1.5, 504)
    ctx = _ctx(_noise(300, 10), n_run=10, n_class=1000)
    with_prior = _bound(ctx).evaluate(val).metrics
    run_only = _bound(ctx, include_prior_runs=False).evaluate(val).metrics
    assert with_prior["n_trials"] == 1000
    assert run_only["n_trials"] == 10
    assert with_prior["dsr"] < run_only["dsr"]


def test_single_trial_equals_psr_against_zero():
    val = _returns(1.0, 504)
    out = _bound(_ctx(_noise(300, 1))).evaluate(val)
    m = return_moments(val)
    expected = psr(m.sharpe, 0.0, m.n, m.skew, m.kurt, m.rho)
    assert out.metrics["dsr"] == pytest.approx(expected)
    assert out.metrics["dsr"] == pytest.approx(out.metrics["psr0"])
    assert out.metrics["sr0_per_bar"] == 0.0


def test_metrics_carry_the_inputs():
    m = _bound(_ctx(_noise(300, 20))).evaluate(_returns(2.0, 504)).metrics
    for key in (
        "n_trials",
        "n_trials_run",
        "n_trials_usable",
        "n_eff",
        "var_sr",
        "sr0_per_bar",
        "sr_per_bar",
        "sharpe_se",
        "skew",
        "kurtosis",
        "rho",
        "n_bars",
        "dsr",
        "psr0",
    ):
        assert key in m, key
    assert m["n_trials"] == 20 and m["n_trials_usable"] == 20
    assert 1.0 <= m["n_eff"] <= 20.0
    assert m["sr0_per_bar"] > 0
    assert m["sharpe_se"] > 0
    assert m["n_bars"] == 504


def test_correlated_trials_have_fewer_effective_trials_than_raw():
    base = _noise(300, 1, seed=3)
    values = base + _noise(300, 30, seed=4) * 0.05  # 30 near-copies
    val = _returns(1.5, 504)
    ranked = _bound(_ctx(values)).evaluate(val).metrics
    raw = _bound(_ctx(values), n_eff_method="raw").evaluate(val).metrics
    assert ranked["n_eff"] < 3.0
    assert raw["n_eff"] == 30.0


def test_unbound_test_fails_with_insufficient_data():
    out = DeflatedSharpeTest().evaluate(_returns(3.0, 504))
    assert out.passed is False
    assert "insufficient data" in out.notes
    assert "bind_run" in out.notes


def test_missing_trial_matrix_fails_with_insufficient_data():
    out = _bound(_ctx(None, n_run=12)).evaluate(_returns(3.0, 504))
    assert out.passed is False
    assert "insufficient data" in out.notes
    assert out.metrics["n_trials"] == 12
    assert math.isnan(out.metrics["dsr"])
    assert out.metrics["sr_per_bar"] > 0  # the strategy side is still reported


def test_no_trials_at_all_fails():
    out = _bound(_ctx(None, n_run=0)).evaluate(_returns(3.0, 504))
    assert out.passed is False
    assert "insufficient data" in out.notes


def test_one_usable_column_among_many_trials_fails_rather_than_skipping_deflation():
    values = np.full((300, 10), np.nan)
    values[:, 0] = _noise(300, 1)[:, 0]
    out = _bound(_ctx(values)).evaluate(_returns(3.0, 504))
    assert out.passed is False
    assert "insufficient data" in out.notes
    assert out.metrics["n_trials_usable"] == 1


def test_all_failed_trials_fail():
    out = _bound(_ctx(np.full((300, 10), np.nan))).evaluate(_returns(3.0, 504))
    assert out.passed is False
    assert "insufficient data" in out.notes


def test_too_few_validation_returns_fail():
    out = _bound(_ctx(_noise(300, 20))).evaluate(np.array([0.01, 0.02]))
    assert out.passed is False
    assert "insufficient data" in out.notes


def test_falls_back_to_the_ledger_matrix():
    values = _noise(300, 20)

    class _Ledger:
        def trial_matrix(self, run_id):
            assert run_id == "run-1"
            return TrialMatrix(index=np.arange(300), values=values)

    ctx = _ctx(values, ledger=_Ledger())
    ctx.trial_matrix = None
    from_ledger = _bound(ctx).evaluate(_returns(3.0, 504))
    direct = _bound(_ctx(values)).evaluate(_returns(3.0, 504))
    assert from_ledger.metrics["dsr"] == pytest.approx(direct.metrics["dsr"])


def test_registry_builds_with_options_and_rejects_bad_ones():
    test = registry.build_survival_test(
        "deflated_sharpe", {"min_dsr": 0.9, "n_eff_method": "avg_corr"}
    )
    assert isinstance(test, DeflatedSharpeTest)
    assert test.min_dsr == 0.9 and test.n_eff_method == "avg_corr"
    for bad in ({"min_dsr": 0.5}, {"min_dsr": 1.0}, {"n_eff_method": "guess"}):
        with pytest.raises(ValueError):
            registry.build_survival_test("deflated_sharpe", bad)

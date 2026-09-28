"""stonks.stats bootstrap, HAC, CSCV/PBO and multiple-testing corrections."""

from __future__ import annotations

import math

import numpy as np
import pytest

from stonks.stats.bootstrap import sharpe_ci, stationary_bootstrap_indices
from stonks.stats.hac import default_lags, newey_west_se
from stonks.stats.multiple_testing import benjamini_hochberg, holm
from stonks.stats.pbo import PBOResult, cscv

# ---- stationary bootstrap -------------------------------------------------------


def test_bootstrap_indices_shape_and_range():
    idx = stationary_bootstrap_indices(100, mean_block=10, n=50, rng=np.random.default_rng(0))
    assert idx.shape == (50, 100)
    assert idx.min() >= 0 and idx.max() < 100


def test_bootstrap_blocks_have_the_requested_mean_length():
    idx = stationary_bootstrap_indices(2000, mean_block=20, n=20, rng=np.random.default_rng(1))
    continues = (np.diff(idx, axis=1) % 2000) == 1
    mean_block = 1 / (1 - continues.mean())
    assert mean_block == pytest.approx(20, rel=0.1)


def test_bootstrap_indices_are_seeded():
    a = stationary_bootstrap_indices(50, 5, 3, np.random.default_rng(9))
    b = stationary_bootstrap_indices(50, 5, 3, np.random.default_rng(9))
    assert (a == b).all()


def test_sharpe_ci_contains_the_point_estimate_and_is_seeded():
    r = np.random.default_rng(3).normal(0.001, 0.01, 500)
    ci = sharpe_ci(r, n_boot=500, seed=4)
    assert ci.lower < ci.sharpe < ci.upper
    assert ci == sharpe_ci(r, n_boot=500, seed=4)


def test_sharpe_ci_coverage_is_about_95_percent_on_iid_normal_data():
    rng = np.random.default_rng(11)
    true_sr, reps, hits = 0.1, 200, 0
    for i in range(reps):
        r = rng.normal(true_sr * 0.01, 0.01, 500)
        ci = sharpe_ci(r, alpha=0.05, n_boot=400, mean_block=5, seed=i)
        hits += ci.lower <= true_sr <= ci.upper
    assert 0.88 <= hits / reps <= 0.99


# ---- Newey-West -----------------------------------------------------------------------


def test_newey_west_with_no_lags_is_the_iid_standard_error():
    x = np.random.default_rng(5).normal(0, 1, 400)
    assert newey_west_se(x, lags=0) == pytest.approx(x.std(ddof=0) / math.sqrt(400))


def test_newey_west_matches_iid_se_when_there_is_no_autocorrelation():
    x = np.random.default_rng(6).normal(0, 1, 20_000)
    assert newey_west_se(x) == pytest.approx(x.std() / math.sqrt(x.size), rel=0.05)


def test_newey_west_widens_under_positive_autocorrelation():
    rng = np.random.default_rng(7)
    e = rng.normal(0, 1, 5000)
    x = np.empty_like(e)
    x[0] = e[0]
    for t in range(1, e.size):
        x[t] = 0.6 * x[t - 1] + e[t]
    assert newey_west_se(x) > 1.5 * x.std() / math.sqrt(x.size)


def test_newey_west_bartlett_weights_by_hand():
    x = np.array([1.0, 3.0, 2.0, 5.0, 4.0])
    d = x - x.mean()
    g0 = (d * d).sum() / 5
    g1 = (d[1:] * d[:-1]).sum() / 5
    expected = math.sqrt((g0 + 2 * (1 - 1 / 2) * g1) / 5)
    assert newey_west_se(x, lags=1) == pytest.approx(expected)


def test_default_lags():
    assert default_lags(100) == 4
    assert default_lags(1000) == math.floor(4 * (10) ** (2 / 9))


# ---- CSCV / PBO -------------------------------------------------------------------------


def test_pbo_is_about_one_half_for_pure_noise():
    m = np.random.default_rng(8).normal(0, 0.01, (1000, 50))
    res = cscv(m, n_blocks=10, seed=0)
    assert isinstance(res, PBOResult)
    assert res.n_combinations == math.comb(10, 5)
    assert 0.3 <= res.pbo <= 0.7


def test_pbo_is_low_when_one_column_dominates():
    rng = np.random.default_rng(9)
    m = rng.normal(0, 0.01, (1000, 30))
    m[:, 7] += 0.004  # a real edge, visible in every block
    res = cscv(m, n_blocks=10, seed=0)
    assert res.pbo < 0.1
    assert res.p_loss < 0.1


def test_pbo_samples_combinations_beyond_the_cap_reproducibly():
    m = np.random.default_rng(10).normal(0, 0.01, (800, 20))
    a = cscv(m, n_blocks=16, max_combinations=200, seed=3)
    b = cscv(m, n_blocks=16, max_combinations=200, seed=3)
    assert a.n_combinations == 200
    assert a == b


def test_pbo_rejects_bad_shapes():
    with pytest.raises(ValueError):
        cscv(np.zeros((100, 5)), n_blocks=7)
    with pytest.raises(ValueError):
        cscv(np.zeros((100, 1)), n_blocks=4)
    with pytest.raises(ValueError):
        cscv(np.zeros((6, 5)), n_blocks=10)


def test_pbo_overfit_selection_degrades_out_of_sample():
    m = np.random.default_rng(12).normal(0, 0.01, (1000, 100))
    assert cscv(m, n_blocks=10, seed=0).degradation_slope < 0.5


# ---- multiple testing ------------------------------------------------------------------

# Benjamini & Hochberg (1995), section 4: 15 p-values from a clinical trial.
BH_PVALUES = [
    0.0001, 0.0004, 0.0019, 0.0095, 0.0201, 0.0278, 0.0298, 0.0344,
    0.0459, 0.3240, 0.4262, 0.5719, 0.6528, 0.7590, 1.0000,
]  # fmt: skip


def test_benjamini_hochberg_textbook_example_rejects_four():
    reject = benjamini_hochberg(BH_PVALUES, q=0.05)
    assert reject.tolist() == [True] * 4 + [False] * 11


def test_holm_on_the_same_example_rejects_three():
    reject = holm(BH_PVALUES, alpha=0.05)
    assert reject.tolist() == [True] * 3 + [False] * 12


def test_corrections_keep_input_order():
    p = [0.04, 0.001, 0.5, 0.01]
    assert holm(p, 0.05).tolist() == [False, True, False, True]
    assert benjamini_hochberg(p, 0.05).tolist() == [False, True, False, True]


def test_bh_step_up_rejects_below_the_largest_passing_rank():
    # p(2) = 0.03 > 2*0.05/3 fails on its own but p(3) passes, so all reject.
    assert benjamini_hochberg([0.01, 0.034, 0.04], 0.05).tolist() == [True, True, True]


def test_empty_input():
    assert holm([], 0.05).tolist() == []
    assert benjamini_hochberg([], 0.05).tolist() == []


def test_stats_package_imports_nothing_from_stonks():
    import pathlib

    import stonks.stats

    for path in pathlib.Path(stonks.stats.__file__).parent.glob("*.py"):
        src = path.read_text()
        assert "from stonks" not in src.replace("from stonks.stats", ""), path.name
        assert "import stonks" not in src.replace("import stonks.stats", ""), path.name


def test_pbo_skips_splits_where_one_half_has_no_usable_trial():
    """Every trial scores -inf in one half: those splits have no in-sample
    winner (or no out-of-sample ranking) and must not count either way."""
    rng = np.random.default_rng(0)
    m = rng.normal(0.0, 0.01, size=(200, 5))
    m[:100, :] = np.nan  # blocks 0 and 1 of 4
    res = cscv(m, n_blocks=4)
    assert res.n_combinations == 4  # C(4, 2) = 6 minus the two degenerate splits
    assert 0.0 <= res.pbo <= 1.0


def test_pbo_is_nan_when_no_split_is_usable():
    m = np.full((40, 3), np.nan)
    res = cscv(m, n_blocks=4)
    assert res.n_combinations == 0
    assert math.isnan(res.pbo) and math.isnan(res.p_loss)


# ---- HAC mean test (roadmap 23.9) ------------------------------------------------


def test_hac_mean_test_known_value_without_lags():
    from stonks.stats.hac import hac_mean_test

    x = np.array([1.0, -1.0, 2.0, 0.0])  # mean 0.5, std(ddof=0) sqrt(1.25)
    out = hac_mean_test(x, lags=0)
    se = math.sqrt(1.25) / 2
    assert out.n == 4
    assert out.se == pytest.approx(se)
    assert out.t_stat == pytest.approx(0.5 / se)
    assert out.p_below == pytest.approx(0.5 * (1 + math.erf((0.5 / se) / math.sqrt(2))))


def test_hac_mean_test_flat_series():
    from stonks.stats.hac import hac_mean_test

    zero = hac_mean_test(np.zeros(10))
    assert zero.t_stat == 0.0 and zero.p_below == pytest.approx(0.5)
    ahead = hac_mean_test(np.full(10, 0.01))
    assert ahead.t_stat > 1e6 and ahead.p_below == pytest.approx(1.0)

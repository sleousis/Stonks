"""White's Reality Check, Hansen's SPA and Romano-Wolf (roadmap 23.9, P2)."""

from __future__ import annotations

import math

import numpy as np
import pytest

from stonks.stats.data_snooping import bootstrap_means, reality_check, romano_wolf, spa

# ---- exact values from hand-picked resamples -------------------------------------

D1 = np.array([[1.0], [2.0], [3.0], [6.0]])  # mean 3
IDX = np.array([[0, 0, 0, 0], [3, 3, 3, 3], [1, 2, 3, 0]])  # resample means 1, 6, 3


def test_bootstrap_means_from_given_indices() -> None:
    means, boot = bootstrap_means(D1, indices=IDX)
    assert means.tolist() == [3.0]
    assert boot[:, 0].tolist() == [1.0, 6.0, 3.0]


def test_reality_check_known_value() -> None:
    # stat = sqrt(4) * 3 = 6; null = 2 * (1-3, 6-3, 3-3) = (-4, 6, 0): one of three >= 6
    out = reality_check(D1, indices=IDX)
    assert out.statistic == pytest.approx(6.0)
    assert out.p_value == pytest.approx(1 / 3)
    assert out.best == 0


def test_spa_known_value_studentized() -> None:
    # omega = sqrt(4 * mean((-2)^2, 3^2, 0^2)) = sqrt(4 * 13/3)
    omega = math.sqrt(4 * 13 / 3)
    out = spa(D1, indices=IDX)
    assert out.statistic == pytest.approx(6.0 / omega)
    # z = 2 * (-2, 3, 0) / omega: only the second reaches the statistic
    assert out.p_upper == pytest.approx(1 / 3)
    assert out.p_consistent == pytest.approx(1 / 3)
    assert out.p_lower == pytest.approx(1 / 3)


def test_romano_wolf_known_values() -> None:
    d = np.array([[1.0, 0.0], [2.0, 1.0], [3.0, -1.0], [6.0, 0.0]])  # means 3, 0
    out = romano_wolf(d, alpha=0.5, studentize=False, indices=IDX)
    # null: model 0 -> 2*(-2, 3, 0) = (-4, 6, 0); model 1 boot means 0, 0, 0 -> (0, 0, 0)
    # stats (6, 0). Step 1 over both: max = (0, 6, 0) >= 6 -> 1/3.
    # Step 2 over model 1: (0, 0, 0) >= 0 -> 1, monotone -> 1.
    assert out.statistics.tolist() == pytest.approx([6.0, 0.0])
    assert out.p_values.tolist() == pytest.approx([1 / 3, 1.0])
    assert out.reject.tolist() == [True, False]
    assert out.n_rejected == 1


def test_one_model_agrees_across_tests() -> None:
    rng = np.random.default_rng(3)
    d = rng.normal(0.02, 1.0, size=(300, 1))
    rc = reality_check(d, n_boot=500, seed=1)
    sp = spa(d, studentize=False, n_boot=500, seed=1)
    rw = romano_wolf(d, studentize=False, n_boot=500, seed=1)
    assert rc.p_value == pytest.approx(sp.p_upper)
    assert rw.p_values[0] == pytest.approx(rc.p_value)


# ---- behaviour -----------------------------------------------------------------


def test_noise_rarely_rejects() -> None:
    """Under the null (every model zero-mean) the Reality Check rejects at
    about its nominal size or less."""
    rng = np.random.default_rng(7)
    rejections = 0
    for rep in range(120):
        d = rng.normal(0.0, 1.0, size=(200, 15))
        rejections += reality_check(d, n_boot=300, mean_block=5, seed=rep).p_value <= 0.05
    assert rejections / 120 <= 0.10


def test_a_real_edge_is_found_and_named() -> None:
    rng = np.random.default_rng(11)
    d = rng.normal(0.0, 1.0, size=(500, 30))
    d[:, 7] += 0.4
    assert reality_check(d, n_boot=500, seed=0).p_value < 0.01
    sp = spa(d, n_boot=500, seed=0)
    assert sp.p_value < 0.01 and sp.best == 7
    rw = romano_wolf(d, n_boot=500, seed=0)
    assert rw.reject[7]
    assert rw.n_rejected <= 2


def test_spa_ignores_poor_models_the_reality_check_counts() -> None:
    """Hansen's point: many clearly bad models inflate White's p-value but
    not the consistent SPA p-value."""
    rng = np.random.default_rng(5)
    good = rng.normal(0.12, 1.0, size=(400, 1))
    bad = rng.normal(-1.0, 3.0, size=(400, 60))
    d = np.hstack([good, bad])
    sp = spa(d, n_boot=1000, seed=2)
    assert sp.p_consistent < sp.p_upper
    assert sp.p_lower <= sp.p_consistent <= sp.p_upper


def test_romano_wolf_p_values_are_monotone_in_the_statistics() -> None:
    rng = np.random.default_rng(1)
    d = rng.normal(0.0, 1.0, size=(250, 12)) + np.linspace(0, 0.3, 12)
    rw = romano_wolf(d, n_boot=400, seed=0)
    order = np.argsort(-rw.statistics)
    assert np.all(np.diff(rw.p_values[order]) >= 0)


def test_seeded_runs_repeat() -> None:
    d = np.random.default_rng(0).normal(size=(100, 4))
    assert spa(d, seed=9).p_value == spa(d, seed=9).p_value


def test_matches_arch_spa() -> None:
    """Known values from an independent implementation: ``arch``'s SPA
    (non-studentized p-values, its own stationary bootstrap) agrees within
    bootstrap noise."""
    from arch.bootstrap import SPA

    rng = np.random.default_rng(21)
    d = rng.normal(0.0, 1.0, size=(500, 8))
    d[:, 2] += 0.06
    ref = SPA(np.zeros(500), -d, block_size=10, reps=4000, seed=4)
    ref.compute()
    ours = spa(d, studentize=False, n_boot=4000, mean_block=10, seed=4)
    assert ours.p_upper == pytest.approx(float(ref.pvalues["upper"]), abs=0.04)
    assert ours.p_consistent == pytest.approx(float(ref.pvalues["consistent"]), abs=0.04)
    assert ours.p_lower == pytest.approx(float(ref.pvalues["lower"]), abs=0.04)


@pytest.mark.parametrize(
    "bad",
    [np.array([[1.0], [np.nan], [2.0]]), np.ones((2, 3)), np.ones((2, 2, 2))],
)
def test_rejects_bad_input(bad: np.ndarray) -> None:
    with pytest.raises(ValueError):
        reality_check(bad)


def test_rejects_out_of_range_indices() -> None:
    with pytest.raises(ValueError):
        bootstrap_means(D1, indices=np.array([[0, 1, 2, 4]]))

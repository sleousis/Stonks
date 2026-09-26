"""The +1-smoothed permutation p-value and the up-front seed draw shared by
the permutation survival tests."""

from __future__ import annotations

import pytest

from stonks.lab.survival.permutation import permutation_p_value, permutation_seeds


def test_p_value_counts_permuted_scores_at_least_as_good_plus_one():
    # two of four permuted scores >= 1.0 (ties count against the strategy)
    assert permutation_p_value(1.0, [0.5, 1.0, 2.0, -1.0]) == pytest.approx(3 / 5)


def test_p_value_is_never_zero():
    assert permutation_p_value(9.0, [0.0, 0.1, 0.2]) == pytest.approx(1 / 4)


def test_p_value_for_a_minimized_objective_counts_lower_scores():
    assert permutation_p_value(1.0, [0.5, 1.0, 2.0], minimize=True) == pytest.approx(3 / 4)


def test_seeds_are_deterministic_and_distinct():
    assert permutation_seeds(5, 17) == permutation_seeds(5, 17)
    assert permutation_seeds(5, 17) != permutation_seeds(5, 18)
    assert len(set(permutation_seeds(50, 1))) == 50
    # a longer run extends a shorter one: seed i doesn't depend on n
    assert permutation_seeds(8, 3)[:5] == permutation_seeds(5, 3)

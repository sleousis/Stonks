"""Effective number of bets and risk contributions (BL-44, P33)."""

from __future__ import annotations

import numpy as np
import pytest

from stonks.portfolio.diversification import effective_number_of_bets, risk_contributions


@pytest.mark.parametrize("n", [1, 2, 5, 10])
def test_enb_is_n_for_identity_and_equal_weights(n: int) -> None:
    assert effective_number_of_bets(np.full(n, 1.0 / n), np.eye(n)) == pytest.approx(n)


def test_enb_is_one_for_a_single_factor() -> None:
    beta = np.array([1.0, 0.5, 2.0, 1.5])
    cov = np.outer(beta, beta) * 0.04  # rank one: every name is the same bet
    assert effective_number_of_bets(np.full(4, 0.25), cov) == pytest.approx(1.0)


def test_enb_of_one_name_is_one() -> None:
    assert effective_number_of_bets(np.array([1.0, 0.0, 0.0]), np.eye(3)) == pytest.approx(1.0)


def test_enb_of_empty_book_is_zero() -> None:
    assert effective_number_of_bets(np.zeros(3), np.eye(3)) == 0.0


def test_risk_contributions_sum_to_one() -> None:
    cov = np.array([[0.04, 0.01], [0.01, 0.09]])
    rc = risk_contributions(np.array([0.6, 0.4]), cov)
    assert rc.sum() == pytest.approx(1.0)
    w = np.array([0.6, 0.4])
    expected = w * (cov @ w) / (w @ cov @ w)
    np.testing.assert_allclose(rc, expected)


def test_shape_mismatch_raises() -> None:
    with pytest.raises(ValueError):
        effective_number_of_bets(np.ones(2), np.eye(3))

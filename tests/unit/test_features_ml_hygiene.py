"""Sample weights on the Classifier seam, bet sizing and the break-even
probability (BL-45)."""

from __future__ import annotations

import math

import numpy as np
import pytest

from stonks.features.ml import ForestClassifier, bet_size, break_even_probability


def test_bet_size_is_zero_at_even_odds_and_signed_around_it():
    assert bet_size(0.5) == 0.0
    assert bet_size(0.6) == pytest.approx(0.2)  # 2*Phi(0.204) - 1 = 0.162, to 0.1 steps
    assert bet_size(0.4) == pytest.approx(-0.2)
    assert bet_size(1.0) == 1.0 and bet_size(0.0) == -1.0


def test_bet_size_without_discretisation_matches_the_formula():
    p = 0.7
    z = (p - 0.5) / math.sqrt(p * (1 - p))
    expected = 2 * 0.5 * (1 + math.erf(z / math.sqrt(2))) - 1
    assert bet_size(p, step=None) == pytest.approx(expected)


def test_bet_size_vectorises_and_handles_more_classes():
    out = bet_size(np.array([0.5, 0.9]), step=None)
    assert out.shape == (2,) and out[0] == 0.0 and out[1] > 0.8
    assert bet_size(1 / 3, n_classes=3) == 0.0


def test_bet_size_rejects_bad_input():
    with pytest.raises(ValueError):
        bet_size(1.2)
    with pytest.raises(ValueError):
        bet_size(0.5, n_classes=1)
    with pytest.raises(ValueError):
        bet_size(0.5, step=0.0)


def test_break_even_probability():
    assert break_even_probability(3.0, 3.0) == 0.5
    assert break_even_probability(2.0, 1.0) == pytest.approx(1 / 3)
    with pytest.raises(ValueError):
        break_even_probability(0.0, 1.0)


def test_forest_classifier_honours_sample_weights():
    rng = np.random.default_rng(0)
    x = rng.normal(size=(200, 1))
    y = (rng.random(200) < 0.5).astype(int)
    # the same x with contradictory labels: the weights decide
    weights = np.where(y == 1, 10.0, 0.1)
    clf = ForestClassifier(n_estimators=50, max_depth=2, seed=0)
    clf.fit(x, y, sample_weight=weights)
    assert clf.predict_proba(x).mean() > 0.8
    with pytest.raises(ValueError):
        clf.fit(x, y, sample_weight=weights[:5])

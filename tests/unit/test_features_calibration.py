"""Probability calibration and conformal abstention (roadmap 23.10)."""

from __future__ import annotations

import json

import numpy as np
import pytest

from stonks.features.calibration import (
    Calibrator,
    ConformalAbstainer,
    IdentityCalibrator,
    IsotonicCalibrator,
    PlattCalibrator,
    ProbabilityPolicy,
    calibrator_from_dict,
    calibrator_kinds,
    make_calibrator,
    purged_oof_proba,
)
from stonks.features.ml import ForestClassifier


def _overconfident(n: int = 2000, seed: int = 0) -> tuple[np.ndarray, np.ndarray]:
    """True P(1) is 0.5 + (p - 0.5) / 3: the model shouts, reality whispers."""
    rng = np.random.default_rng(seed)
    p = rng.uniform(0.02, 0.98, n)
    truth = 0.5 + (p - 0.5) / 3.0
    y = (rng.uniform(size=n) < truth).astype(int)
    return p, y


def _brier(p: np.ndarray, y: np.ndarray) -> float:
    return float(np.mean((p - y) ** 2))


def test_registry_names_every_calibrator():
    assert set(calibrator_kinds()) >= {"none", "isotonic", "platt"}
    assert isinstance(make_calibrator("platt"), PlattCalibrator)
    with pytest.raises(ValueError, match="unknown calibrator"):
        make_calibrator("magic")


@pytest.mark.parametrize("kind", ["isotonic", "platt"])
def test_calibrators_shrink_an_overconfident_model(kind):
    p, y = _overconfident()
    cal = make_calibrator(kind)
    cal.fit(p[:1000], y[:1000])
    out = cal.transform(p[1000:])
    assert _brier(out, y[1000:]) < _brier(p[1000:], y[1000:])
    assert np.all((out >= 0) & (out <= 1))
    assert out[np.argmax(p[1000:])] < 0.9


@pytest.mark.parametrize("kind", ["none", "isotonic", "platt"])
def test_calibrators_round_trip_through_json(kind):
    p, y = _overconfident(400)
    cal = make_calibrator(kind)
    cal.fit(p, y)
    payload = json.loads(json.dumps(cal.to_dict()))
    again = calibrator_from_dict(payload)
    assert type(again) is type(cal)
    np.testing.assert_allclose(again.transform(p), cal.transform(p))


def test_isotonic_is_monotone_and_clips_outside_the_fit_range():
    p, y = _overconfident(500)
    cal = IsotonicCalibrator()
    cal.fit(np.clip(p, 0.2, 0.8), y)
    grid = np.linspace(0, 1, 101)
    out = cal.transform(grid)
    assert np.all(np.diff(out) >= -1e-12)
    assert out[0] == out[20]


def test_identity_is_a_no_op():
    p = np.array([0.1, 0.5, 0.9])
    cal = IdentityCalibrator()
    cal.fit(p, np.array([0, 1, 1]))
    np.testing.assert_array_equal(cal.transform(p), p)


def test_transform_before_fit_raises():
    with pytest.raises(RuntimeError):
        PlattCalibrator().transform(np.array([0.5]))


def test_calibrator_is_an_abc():
    assert issubclass(PlattCalibrator, Calibrator)


def test_calibrator_rejects_bad_input():
    with pytest.raises(ValueError):
        PlattCalibrator().fit(np.array([0.2, 1.5]), np.array([0, 1]))
    with pytest.raises(ValueError):
        IsotonicCalibrator().fit(np.array([0.2]), np.array([0, 1]))


# ---- conformal abstention --------------------------------------------------


def test_conformal_abstains_on_the_ambiguous_middle_only():
    p, y = _overconfident(3000)
    conf = ConformalAbstainer(alpha=0.35)
    conf.fit(p, y)
    abstain = conf.abstain(np.array([0.02, 0.5, 0.98]))
    assert list(abstain) == [False, True, False]


def test_conformal_prediction_sets_hold_their_coverage():
    p, y = _overconfident(4000, seed=3)
    conf = ConformalAbstainer(alpha=0.2)
    conf.fit(p[:2000], y[:2000])
    has0, has1 = conf.prediction_set(p[2000:])
    covered = np.where(y[2000:] == 1, has1, has0)
    assert covered.mean() >= 0.8 - 0.03


def test_conformal_with_too_few_points_always_abstains():
    conf = ConformalAbstainer(alpha=0.05)
    conf.fit(np.array([0.9, 0.1, 0.8]), np.array([1, 0, 1]))
    assert conf.abstain(np.array([0.99, 0.01])).all()


def test_conformal_round_trips_and_rejects_bad_alpha():
    p, y = _overconfident(300)
    conf = ConformalAbstainer(alpha=0.3)
    conf.fit(p, y)
    again = ConformalAbstainer.from_dict(json.loads(json.dumps(conf.to_dict())))
    np.testing.assert_array_equal(again.abstain(p), conf.abstain(p))
    with pytest.raises(ValueError):
        ConformalAbstainer(alpha=0.0)
    with pytest.raises(ValueError):
        ConformalAbstainer(alpha=1.0)


# ---- purged out-of-fold predictions ---------------------------------------------


def _toy(n: int = 120, seed: int = 0):
    rng = np.random.default_rng(seed)
    x = rng.normal(size=(n, 3))
    y = (x[:, 0] + rng.normal(0, 0.7, n) > 0).astype(int)
    t0 = np.arange(n)
    t1 = t0 + 3
    return x, y, t0, t1


def test_oof_predictions_come_from_models_that_never_saw_the_row():
    x, y, t0, t1 = _toy()
    row_of = {float(v): i for i, v in enumerate(x[:, 0])}
    seen: list[set[int]] = []

    class Spy(ForestClassifier):
        def fit(self, xx, yy, sample_weight=None):
            seen.append({row_of[float(v)] for v in xx[:, 0]})
            super().fit(xx, yy, sample_weight)

    oof = purged_oof_proba(lambda: Spy(n_estimators=10), x, y, t0, t1, folds=4)
    assert oof.shape == (len(y),)
    assert np.isfinite(oof).all()
    for train_rows, lo in zip(seen, range(0, 120, 30), strict=True):
        test_rows = set(range(lo, lo + 30))
        assert not train_rows & test_rows
        # purge: labels that overlap the test block are gone too
        assert not train_rows & set(range(max(0, lo - 3), lo))


def test_oof_prediction_of_a_row_never_reads_its_own_label():
    """Flipping the first fold's labels leaves that fold's predictions alone:
    they come from models trained on the other, purged folds only."""
    x, y, t0, t1 = _toy()
    base = purged_oof_proba(lambda: ForestClassifier(n_estimators=20), x, y, t0, t1, folds=4)
    shocked_y = y.copy()
    shocked_y[:30] = 1 - shocked_y[:30]  # the first fold's own labels
    again = purged_oof_proba(
        lambda: ForestClassifier(n_estimators=20), x, shocked_y, t0, t1, folds=4
    )
    np.testing.assert_allclose(base[:30], again[:30])


# ---- the policy seam ------------------------------------------------------------


def test_policy_fits_on_oof_and_zeroes_abstained_bets():
    p, y = _overconfident(2000)
    policy = ProbabilityPolicy.fit("platt", p, y, conformal_alpha=0.35)
    decision = policy.apply(np.array([0.02, 0.5, 0.98]))
    assert list(decision.abstain) == [False, True, False]
    assert decision.probability[2] < 0.98
    sizes = policy.bet_size(np.array([0.5, 0.98]))
    assert sizes[0] == 0.0
    assert sizes[1] > 0.0


def test_policy_without_conformal_never_abstains_and_round_trips():
    p, y = _overconfident(500)
    policy = ProbabilityPolicy.fit("platt", p, y)
    assert not policy.apply(p).abstain.any()
    again = ProbabilityPolicy.from_dict(json.loads(json.dumps(policy.to_dict())))
    np.testing.assert_allclose(again.apply(p).probability, policy.apply(p).probability)
    assert again.to_dict() == policy.to_dict()


def test_policy_ignores_unscored_oof_rows():
    p, y = _overconfident(500)
    p = p.copy()
    p[:50] = np.nan
    policy = ProbabilityPolicy.fit("platt", p, y)
    assert policy.n_fit == 450

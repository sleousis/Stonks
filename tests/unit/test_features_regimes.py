"""Latent regime model: Markov switching fitted with statsmodels, filtered
with our own Hamilton filter (BL-46)."""

from __future__ import annotations

import numpy as np
import pytest

from stonks.features.regimes import MarkovSwitchingRegime, RegimeParams, hamilton_filter

P_TRUE = np.array([[0.98, 0.02], [0.05, 0.95]])
MU_TRUE = np.array([0.0005, -0.001])
SIGMA_TRUE = np.array([0.008, 0.025])


def _simulate(n=2000, seed=0):
    rng = np.random.default_rng(seed)
    states = [0]
    for _ in range(n - 1):
        states.append(int(rng.choice(2, p=P_TRUE[states[-1]])))
    s = np.array(states)
    r = rng.normal(MU_TRUE[s], SIGMA_TRUE[s])
    return r, s


def test_filter_with_the_true_params_finds_the_regimes():
    r, s = _simulate()
    params = RegimeParams(means=MU_TRUE, sigmas=SIGMA_TRUE, transition=P_TRUE)
    probs = hamilton_filter(r, params)
    assert probs.shape == (len(r), 2)
    np.testing.assert_allclose(probs.sum(axis=1), 1.0)
    accuracy = ((probs[:, 1] > 0.5).astype(int) == s).mean()
    assert accuracy > 0.9


def test_filter_uses_no_future_bars():
    r, _ = _simulate(500, seed=3)
    params = RegimeParams(means=MU_TRUE, sigmas=SIGMA_TRUE, transition=P_TRUE)
    full = hamilton_filter(r, params)
    for t in (10, 100, 499):
        np.testing.assert_allclose(hamilton_filter(r[: t + 1], params)[-1], full[t])


def test_fit_recovers_the_regimes_and_orders_states_by_volatility():
    r, s = _simulate()
    model = MarkovSwitchingRegime(k=2).fit(r)
    params = model.params
    assert params is not None
    assert params.sigmas[0] < params.sigmas[1]
    assert params.sigmas[1] == pytest.approx(SIGMA_TRUE[1], rel=0.2)
    assert params.sigmas[0] == pytest.approx(SIGMA_TRUE[0], rel=0.2)
    np.testing.assert_allclose(params.transition.sum(axis=1), 1.0)
    assert model.high_vol_state == 1
    probs = model.filtered_probs(r)
    assert ((probs[:, 1] > 0.5).astype(int) == s).mean() > 0.9
    assert model.high_vol_probability(r) == pytest.approx(probs[-1, 1])


def test_params_round_trip_through_plain_data():
    r, _ = _simulate(800, seed=5)
    model = MarkovSwitchingRegime().fit(r)
    again = MarkovSwitchingRegime.from_dict(model.to_dict())
    np.testing.assert_allclose(again.filtered_probs(r), model.filtered_probs(r))
    assert isinstance(model.to_dict()["means"], list)  # JSON-ready, no library types


def test_unfitted_model_and_bad_input_are_refused():
    model = MarkovSwitchingRegime()
    with pytest.raises(RuntimeError):
        model.filtered_probs(np.zeros(10))
    with pytest.raises(ValueError):
        model.fit(np.zeros(5))
    with pytest.raises(ValueError):
        MarkovSwitchingRegime(k=1)
    with pytest.raises(ValueError):
        RegimeParams(means=np.zeros(2), sigmas=np.array([0.1, -0.1]), transition=P_TRUE)


def test_filter_survives_an_extreme_outlier():
    params = RegimeParams(means=MU_TRUE, sigmas=SIGMA_TRUE, transition=P_TRUE)
    r = np.array([0.0, 0.001, 5.0, 0.0])
    probs = hamilton_filter(r, params)
    assert np.all(np.isfinite(probs))
    assert probs[2, 1] > 0.99

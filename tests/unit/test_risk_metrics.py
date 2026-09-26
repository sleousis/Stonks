"""Live VaR and expected shortfall, violation ratio and the Kupiec test
(BL-47, roadmap 9.5.4): the pure math in ``production.risk_metrics``."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest
from scipy.stats import norm

from stonks.production.risk_metrics import (
    RiskForecast,
    book_forecast,
    ewma_sigma,
    kupiec_pof,
    parametric_var_es,
    violation_ratio,
)


def test_parametric_var_and_es_of_a_known_sigma():
    var95, es95 = parametric_var_es(0.01, 0.95)
    assert var95 == pytest.approx(0.01 * norm.ppf(0.95))
    assert es95 == pytest.approx(0.01 * norm.pdf(norm.ppf(0.95)) / 0.05)
    var99, es99 = parametric_var_es(0.01, 0.99)
    assert var99 == pytest.approx(0.023263, abs=1e-6)
    assert es99 > var99 > var95


def test_parametric_var_rejects_a_bad_level():
    with pytest.raises(ValueError, match="level"):
        parametric_var_es(0.01, 1.0)


def test_ewma_sigma_recovers_a_known_sigma():
    rng = np.random.default_rng(7)
    returns = pd.DataFrame({"A": rng.normal(0.0, 0.02, 250)})
    sigma = ewma_sigma(returns, {"A": 1.0}, lam=0.94)
    assert sigma == pytest.approx(0.02, rel=0.35)


def test_ewma_sigma_of_two_perfectly_hedged_names_is_zero():
    r = np.random.default_rng(1).normal(0.0, 0.01, 100)
    returns = pd.DataFrame({"A": r, "B": r})
    assert ewma_sigma(returns, {"A": 0.5, "B": -0.5}) == pytest.approx(0.0, abs=1e-6)


def test_ewma_sigma_ignores_names_without_weight_or_history():
    returns = pd.DataFrame({"A": [0.01, -0.01, 0.02, -0.02]})
    assert ewma_sigma(returns, {"A": 1.0, "ZZZ": 0.3}) == ewma_sigma(returns, {"A": 1.0})
    assert ewma_sigma(returns, {}) == 0.0


def test_book_forecast_scales_by_weight_and_keeps_cash_riskless():
    rng = np.random.default_rng(3)
    closes = {"A": pd.Series(100 * np.exp(np.cumsum(rng.normal(0, 0.01, 260))))}
    full = book_forecast(closes, {"A": 1000.0}, 1000.0)
    half = book_forecast(closes, {"A": 500.0}, 1000.0)
    assert isinstance(full, RiskForecast)
    assert half.var_95 == pytest.approx(full.var_95 / 2)
    assert half.es_99 == pytest.approx(full.es_99 / 2)
    assert full.observations == 250
    cash = book_forecast(closes, {}, 1000.0)
    assert cash.var_95 == 0.0 and cash.sigma == 0.0


def test_book_forecast_of_an_empty_book_is_none():
    assert book_forecast({}, {"A": 1.0}, 0.0) is None


def test_violation_ratio_counts():
    assert violation_ratio(5, 100, 0.05) == pytest.approx(1.0)
    assert violation_ratio(10, 100, 0.05) == pytest.approx(2.0)
    assert violation_ratio(0, 0, 0.05) is None


def test_kupiec_accepts_the_expected_rate_and_rejects_too_many():
    assert kupiec_pof(5, 100, 0.05) == pytest.approx(1.0)
    assert kupiec_pof(20, 100, 0.05) < 0.001
    # zero violations over a long window is also a rejection (too cautious)
    assert kupiec_pof(0, 250, 0.05) < 0.001
    assert kupiec_pof(0, 0, 0.05) is None


def test_kupiec_matches_the_textbook_statistic():
    x, t, p = 8, 250, 0.01
    pi = x / t
    lr = -2 * ((t - x) * math.log(1 - p) + x * math.log(p)) + 2 * (
        (t - x) * math.log(1 - pi) + x * math.log(pi)
    )
    from scipy.stats import chi2

    assert kupiec_pof(x, t, p) == pytest.approx(float(chi2.sf(lr, 1)))

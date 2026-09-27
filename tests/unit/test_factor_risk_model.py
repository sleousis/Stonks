"""Factor risk models (roadmap 22.4): PCA and style models, their
covariance estimators and the construction pipeline passing exposures."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from stonks.core.types import Portfolio
from stonks.portfolio._risk_based import RiskBasedSettings, covariance_for
from stonks.portfolio.base import ConstructionInput, get_constructor
from stonks.portfolio.covariance import estimator_names, get_estimator
from stonks.portfolio.factor_model import (
    MARKET,
    fit_pca_model,
    fit_style_model,
    model_exposures,
    returns_style_exposures,
    standardize_exposures,
)

TICKERS = [f"T{i}" for i in range(12)]


def _one_factor(n_rows: int = 2_000, seed: int = 0) -> tuple[pd.DataFrame, np.ndarray]:
    rng = np.random.default_rng(seed)
    beta = np.linspace(0.5, 1.5, len(TICKERS))
    market = rng.normal(0, 0.01, n_rows)
    noise = rng.normal(0, 0.005, (n_rows, len(TICKERS)))
    true = np.outer(beta, beta) * 0.01**2 + np.eye(len(TICKERS)) * 0.005**2
    return pd.DataFrame(np.outer(market, beta) + noise, columns=TICKERS), true


def _style(n_rows: int = 3_000, seed: int = 1):
    """Returns from a market plus two styles with known exposures."""
    rng = np.random.default_rng(seed)
    n = len(TICKERS)
    raw = pd.DataFrame({"momentum": rng.normal(size=n), "size": rng.normal(size=n)}, index=TICKERS)
    b = standardize_exposures(raw).to_numpy()
    f = np.column_stack(
        [rng.normal(0, 0.01, n_rows), rng.normal(0, 0.006, n_rows), rng.normal(0, 0.003, n_rows)]
    )
    design = np.column_stack([np.ones(n), b])
    x = f @ design.T + rng.normal(0, 0.004, (n_rows, n))
    true = design @ np.diag([0.01**2, 0.006**2, 0.003**2]) @ design.T + np.eye(n) * 0.004**2
    return pd.DataFrame(x, columns=TICKERS), raw, true


# ---- exposures -----------------------------------------------------------------


def test_standardize_zscores_fills_missing_and_one_hots_sectors():
    raw = pd.DataFrame(
        {
            "momentum": [0.1, 0.2, np.nan, 0.4],
            "size": [5.0, 5.0, 5.0, 5.0],  # no spread: dropped
            "sector": ["Tech", "Tech", None, "Energy"],
        },
        index=list("ABCD"),
    )
    out = standardize_exposures(raw)
    assert "size" not in out
    assert out.loc["C", "momentum"] == 0.0
    known = out.loc[["A", "B", "D"], "momentum"]
    assert known.is_monotonic_increasing
    assert list(out.filter(like="sector:").columns) == ["sector:Energy", "sector:Tech"]
    assert out.loc["C", ["sector:Energy", "sector:Tech"]].sum() == 0.0
    assert out.loc["A", "sector:Tech"] == 1.0


def test_standardize_clips_outliers():
    raw = pd.DataFrame({"value": [0.0, 0.1, -0.1, 0.05, -0.05, 100.0]})
    out = standardize_exposures(raw)["value"]
    # unclipped, the outlier squeezes the other five into one point
    assert out.iloc[:5].std() > 0.3


def test_returns_style_exposures_skip_the_last_month():
    rows = 100
    r = pd.DataFrame({"A": [0.01] * rows, "B": [0.0] * (rows - 21) + [0.05] * 21})
    got = returns_style_exposures(r)
    assert got.loc["A", "momentum"] == pytest.approx(1.01 ** (rows - 21) - 1)
    assert got.loc["B", "momentum"] == pytest.approx(0.0)
    assert got.loc["B", "volatility"] > got.loc["A", "volatility"]


# ---- PCA --------------------------------------------------------------------------


def test_pca_recovers_a_one_factor_covariance():
    r, true = _one_factor()
    model = fit_pca_model(r.to_numpy(), n_factors=1, tickers=TICKERS)
    assert model.factors == ("pc1",)
    cov = model.covariance()
    assert np.abs(cov - true).max() < 0.15 * true.max()
    assert np.all(model.specific_var > 0)
    assert np.all(model.exposures[:, 0] > 0)  # the sign is fixed


def test_pca_clips_factor_count_to_the_data():
    r = pd.DataFrame(np.random.default_rng(0).normal(size=(3, 5)))
    model = fit_pca_model(r.to_numpy(), n_factors=10)
    assert len(model.factors) == 2


# ---- style -------------------------------------------------------------------------


def test_style_model_recovers_the_true_covariance():
    r, raw, true = _style()
    b = standardize_exposures(raw)
    model = fit_style_model(r.to_numpy(), b.to_numpy(), list(b.columns), tickers=TICKERS)
    assert model.factors == (MARKET, "momentum", "size")
    assert np.abs(model.covariance() - true).max() < 0.1 * true.max()
    assert model.factor_cov[1, 1] == pytest.approx(0.006**2, rel=0.15)
    split = model.risk_split(dict.fromkeys(TICKERS, 1 / len(TICKERS)))
    assert split["factor"] > split["specific"] > 0
    exposure = model.exposure_of(dict.fromkeys(TICKERS, 1 / len(TICKERS)))
    assert exposure[MARKET] == pytest.approx(1.0)
    assert exposure["momentum"] == pytest.approx(0.0, abs=1e-12)


def test_style_model_drops_collinear_sector_dummies():
    r, raw, _ = _style(n_rows=200)
    raw = raw.assign(sector=["A"] * 6 + ["B"] * 6)
    b = standardize_exposures(raw)
    model = fit_style_model(r.to_numpy(), b.to_numpy(), list(b.columns), tickers=TICKERS)
    # market + A + B are collinear: one dummy goes
    assert len([f for f in model.factors if f.startswith("sector:")]) == 1


def test_style_model_adds_residual_components():
    r, raw, _ = _style(n_rows=300)
    b = standardize_exposures(raw)
    model = fit_style_model(r.to_numpy(), b.to_numpy(), list(b.columns), residual_pcs=2)
    assert model.factors[-2:] == ("pc1", "pc2")
    assert model.exposures.shape == (len(TICKERS), 5)


def test_model_exposures_fall_back_to_the_returns():
    r, raw, _ = _style(n_rows=100)
    derived = model_exposures(TICKERS, r, None)
    assert set(derived.columns) == {"momentum", "volatility"}
    given = model_exposures(TICKERS, r, raw.drop(index="T0"))
    assert given.loc["T0", "size"] == 0.0
    assert {"momentum", "size", "volatility"} <= set(given.columns)


# ---- estimators ----------------------------------------------------------------------


def test_pca_and_style_estimators_are_registered():
    assert {"pca", "style"} <= set(estimator_names())
    with pytest.raises(ValueError):
        get_estimator("pca", n_factors=0)
    with pytest.raises(ValueError):
        get_estimator("style", residual_pcs=-1)


def test_style_estimator_uses_the_given_exposures():
    r, raw, true = _style()
    est = get_estimator("style").estimate(r, exposures=raw)
    assert np.abs(est - true).max() < 0.1 * true.max()
    assert np.all(np.linalg.eigvalsh(est) > 0)


def test_style_estimator_without_exposures_reads_the_returns():
    r, _, _ = _style(n_rows=300)
    est = get_estimator("style").estimate(r)
    assert est.shape == (len(TICKERS), len(TICKERS))
    assert np.allclose(est, est.T)


def test_every_estimator_accepts_exposures():
    r, raw, _ = _style(n_rows=200)
    for name in estimator_names():
        assert get_estimator(name).estimate(r, exposures=raw).shape == (12, 12)


def test_pca_estimator_is_psd_with_more_names_than_rows():
    r = pd.DataFrame(np.random.default_rng(3).normal(0, 0.01, (8, 12)), columns=TICKERS)
    est = get_estimator("pca", n_factors=2).estimate(r)
    assert np.all(np.linalg.eigvalsh(est) > 0)


# ---- construction ----------------------------------------------------------------------


def _input(r: pd.DataFrame, exposures: pd.DataFrame | None, as_of: date) -> ConstructionInput:
    return ConstructionInput(
        signals={"s": dict.fromkeys(TICKERS, 1.0)},
        portfolio=Portfolio(cash=100_000.0),
        prices=dict.fromkeys(TICKERS, 10.0),
        as_of=as_of,
        returns_history=r,
        factor_exposures=exposures,
    )


def test_covariance_for_passes_exposures_to_the_estimator():
    r, raw, _ = _style(n_rows=400)
    r.index = pd.bdate_range("2020-01-01", periods=len(r))
    as_of = r.index[-1].date()
    settings = RiskBasedSettings(estimator="style", top_n=12, lookback=400)
    with_exp = covariance_for(_input(r, raw, as_of), TICKERS, settings)
    without = covariance_for(_input(r, None, as_of), TICKERS, settings)
    assert with_exp.source == "history"
    assert not np.allclose(with_exp.matrix, without.matrix)


def test_covariance_for_reads_no_row_after_the_decision():
    r, raw, _ = _style(n_rows=400)
    r.index = pd.bdate_range("2020-01-01", periods=len(r))
    as_of = r.index[300].date()
    settings = RiskBasedSettings(estimator="style", top_n=12, lookback=400)
    base = covariance_for(_input(r, raw, as_of), TICKERS, settings).matrix
    future = r.copy()
    future.iloc[301:] *= 50.0
    assert np.allclose(covariance_for(_input(future, raw, as_of), TICKERS, settings).matrix, base)


def test_an_optimiser_runs_on_the_style_model():
    r, raw, _ = _style(n_rows=300)
    r.index = pd.bdate_range("2020-01-01", periods=len(r))
    hrp = get_constructor("erc", estimator="style", top_n=12, lookback=300)
    book = hrp.target_weights(_input(r, raw, r.index[-1].date()))
    assert sum(book.weights.values()) == pytest.approx(1.0)
    assert book.meta["covariance"] == "history"


def test_estimator_params_reach_the_estimator():
    settings = RiskBasedSettings(estimator="pca", estimator_params={"n_factors": 2})
    assert settings.estimator_params == {"n_factors": 2}
    with pytest.raises(ValueError):
        RiskBasedSettings(estimator="pca", estimator_params={"nope": 1})

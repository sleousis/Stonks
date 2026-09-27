"""Factor risk models (roadmap 22.4): pure numpy and pandas.

A factor model writes the covariance of ``N`` names as

    S = B F B' + D

``B`` is ``N x K`` exposures, ``F`` the ``K x K`` covariance of the factor
returns and ``D`` the diagonal of specific (idiosyncratic) variances. With
``K`` much smaller than ``N`` the estimate is stable where a sample
covariance is noise.

Two ways to get ``B``:

- :func:`fit_pca_model`: statistical factors. ``B`` is the top ``K``
  eigenvectors of the sample covariance, the factor returns are the
  returns projected on them.
- :func:`fit_style_model`: style factors. ``B`` is given (momentum, size,
  value, volatility, sector), standardised by :func:`standardize_exposures`.
  Each row of returns is regressed on ``[1, B]`` (the constant is the
  market), which gives one factor return per bar. ``residual_pcs`` adds
  that many principal components of the residuals as extra factors.

Point in time: a model is fit on the returns and exposures it is handed.
Callers pass rows dated up to the decision only and exposures known at it
(P12). Applying today's exposures to past rows is the usual simplification
of a cross-sectional model. It uses nothing after the decision.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np
import pandas as pd

__all__ = [
    "MARKET",
    "STYLE_FACTORS",
    "FactorModel",
    "cross_section_returns",
    "fit_pca_model",
    "fit_style_model",
    "model_exposures",
    "returns_style_exposures",
    "standardize_exposures",
]

#: The style factors every model and rule knows by name.
STYLE_FACTORS: tuple[str, ...] = ("momentum", "size", "value", "volatility")
#: Name of the intercept factor of a style model.
MARKET = "market"
#: Column that holds a sector label; it becomes one dummy per sector.
SECTOR = "sector"
#: Exposures are clipped at this many standard deviations.
WINSOR_Z = 3.0
#: Floor of a specific variance, relative to the mean total variance.
SPECIFIC_FLOOR = 1e-6
#: Bars skipped at the end of the momentum window (short-term reversal).
MOMENTUM_SKIP = 21


@dataclass(frozen=True)
class FactorModel:
    """``S = B F B' + D`` over ``tickers`` (see the module doc)."""

    tickers: tuple[str, ...]
    factors: tuple[str, ...]
    #: ``N x K`` exposures.
    exposures: np.ndarray
    #: ``K x K`` factor covariance, per bar.
    factor_cov: np.ndarray
    #: ``N`` specific variances, per bar.
    specific_var: np.ndarray

    def covariance(self) -> np.ndarray:
        b = self.exposures
        return b @ self.factor_cov @ b.T + np.diag(self.specific_var)

    def weights_vector(self, weights: Mapping[str, float]) -> np.ndarray:
        return np.array([float(weights.get(t, 0.0)) for t in self.tickers])

    def exposure_of(self, weights: Mapping[str, float]) -> dict[str, float]:
        """``B' w``: the book's exposure to each factor."""
        w = self.weights_vector(weights)
        return dict(zip(self.factors, (self.exposures.T @ w).tolist(), strict=True))

    def risk_split(self, weights: Mapping[str, float]) -> dict[str, float]:
        """The book's variance per bar, split into the factor part
        ``w' B F B' w`` and the specific part ``w' D w``."""
        w = self.weights_vector(weights)
        x = self.exposures.T @ w
        factor = float(x @ self.factor_cov @ x)
        specific = float(w**2 @ self.specific_var)
        return {"factor": factor, "specific": specific, "total": factor + specific}


# ---- exposures ---------------------------------------------------------------------


def standardize_exposures(raw: pd.DataFrame) -> pd.DataFrame:
    """Raw exposures (rows tickers) as model exposures.

    - A numeric column is clipped at ``WINSOR_Z`` robust z-scores, then
      z-scored across the rows (mean 0, standard deviation 1). A missing
      value becomes 0, the average name.
    - The ``sector`` column becomes one 0/1 dummy per sector
      (``sector:<name>``). A name without a sector gets zeros.
    - A column with no spread (all equal or all missing) is dropped.
    """
    out: dict[str, pd.Series] = {}
    for column in raw.columns:
        values = raw[column]
        if column == SECTOR:
            labels = values.where(values.notna(), None)
            for name in sorted({str(v) for v in labels.dropna() if str(v)}):
                out[f"{SECTOR}:{name}"] = (labels.astype(str) == name).astype(float)
            continue
        x = pd.to_numeric(values, errors="coerce").astype(float)
        x = x.where(np.isfinite(x))
        finite = x.dropna()
        if finite.size < 2 or float(finite.std(ddof=0)) <= 0:
            continue
        median = float(finite.median())
        mad = float((finite - median).abs().median()) * 1.4826
        scale = mad if mad > 0 else float(finite.std(ddof=0))
        x = x.clip(median - WINSOR_Z * scale, median + WINSOR_Z * scale)
        std = float(x.std(ddof=0))
        if not std > 0:
            continue
        out[str(column)] = ((x - x.mean()) / std).fillna(0.0)
    frame = pd.DataFrame(out, index=raw.index)
    if SECTOR in raw.columns:
        # a sector dummy that every name shares is the market itself
        frame = frame.loc[:, frame.nunique() > 1]
    return frame


def returns_style_exposures(returns: pd.DataFrame) -> pd.DataFrame:
    """Momentum and volatility read from a return table alone (rows bars,
    oldest first): the compounded return up to ``MOMENTUM_SKIP`` bars
    before the end (the whole table when it is short) and the standard
    deviation. Raw values, rows tickers."""
    x = returns.astype(float)
    window = x.iloc[:-MOMENTUM_SKIP] if len(x) > 2 * MOMENTUM_SKIP else x
    momentum = np.expm1(np.log1p(window.clip(lower=-0.99)).sum())
    return pd.DataFrame({"momentum": momentum, "volatility": x.std(ddof=1)})


# ---- fitting -----------------------------------------------------------------------


def _design(b: np.ndarray, factor_names: Sequence[str]) -> tuple[np.ndarray, list[str]]:
    """``[1, B]`` with the market in front, less any column collinear with
    the ones before it (a full set of sector dummies is the market)."""
    design = np.column_stack([np.ones(b.shape[0]), b])
    labels = [MARKET, *factor_names]
    if np.linalg.matrix_rank(design) < design.shape[1]:
        keep: list[int] = []
        for j in range(design.shape[1]):
            if np.linalg.matrix_rank(design[:, [*keep, j]]) == len(keep) + 1:
                keep.append(j)
        design = design[:, keep]
        labels = [labels[j] for j in keep]
    return design, labels


def cross_section_returns(returns: pd.Series, exposures: pd.DataFrame) -> dict[str, float]:
    """One bar's factor returns: ``returns`` (index tickers) regressed on
    ``[1, exposures]`` over the names that have a return. With fewer names
    than factors plus three only the market (the mean return) is given;
    with no name, nothing."""
    r = pd.to_numeric(returns, errors="coerce").astype(float)
    r = r[np.isfinite(r)]
    if r.empty:
        return {}
    b = exposures.reindex(r.index).fillna(0.0)
    if len(r) < b.shape[1] + 3:
        return {MARKET: float(r.mean())}
    design, labels = _design(b.to_numpy(dtype=float), [str(c) for c in b.columns])
    coef, *_ = np.linalg.lstsq(design, r.to_numpy(), rcond=None)
    return dict(zip(labels, coef.tolist(), strict=True))


def _specific(residuals: np.ndarray, total_var: np.ndarray) -> np.ndarray:
    var = np.var(residuals, axis=0, ddof=1) if residuals.shape[0] > 1 else np.zeros(0)
    floor = SPECIFIC_FLOOR * max(float(np.mean(total_var)), 1e-18)
    return np.maximum(np.nan_to_num(var, nan=floor), floor)


def _top_components(cov: np.ndarray, k: int) -> tuple[np.ndarray, np.ndarray]:
    values, vectors = np.linalg.eigh(0.5 * (cov + cov.T))
    order = np.argsort(values)[::-1][:k]
    vectors = vectors[:, order]
    # a stable sign: the largest loading of each component is positive
    signs = np.sign(vectors[np.abs(vectors).argmax(axis=0), np.arange(vectors.shape[1])])
    return values[order], vectors * np.where(signs == 0, 1.0, signs)


def fit_pca_model(
    returns: np.ndarray, n_factors: int = 3, tickers: Sequence[str] | None = None
) -> FactorModel:
    """Statistical factor model: the top ``n_factors`` principal components
    of the sample covariance (at most ``min(N, T) - 1``), with the variance
    they leave as the specific part."""
    x = np.asarray(returns, dtype=float)
    n_rows, n_cols = x.shape
    names = tuple(tickers) if tickers is not None else tuple(str(i) for i in range(n_cols))
    centred = x - x.mean(axis=0)
    cov = np.atleast_2d(np.cov(centred, rowvar=False, ddof=1))
    k = max(0, min(int(n_factors), n_cols - 1, n_rows - 1))
    if k == 0:
        return FactorModel(names, (), np.zeros((n_cols, 0)), np.zeros((0, 0)), np.diag(cov).copy())
    _, loadings = _top_components(cov, k)
    factor_returns = centred @ loadings
    factor_cov = np.atleast_2d(np.cov(factor_returns, rowvar=False, ddof=1))
    residuals = centred - factor_returns @ loadings.T
    specific = _specific(residuals, np.diag(cov))
    return FactorModel(names, tuple(f"pc{i + 1}" for i in range(k)), loadings, factor_cov, specific)


def fit_style_model(
    returns: np.ndarray,
    exposures: np.ndarray,
    factor_names: Sequence[str],
    *,
    tickers: Sequence[str] | None = None,
    residual_pcs: int = 0,
) -> FactorModel:
    """Style factor model (module doc). ``exposures`` is ``N x K`` and
    already standardised; the market factor is added in front."""
    x = np.asarray(returns, dtype=float)
    n_rows, n_cols = x.shape
    names = tuple(tickers) if tickers is not None else tuple(str(i) for i in range(n_cols))
    b = np.asarray(exposures, dtype=float).reshape(n_cols, -1)
    design, labels = _design(b, factor_names)
    factor_returns, *_ = np.linalg.lstsq(design, x.T, rcond=None)  # (K+1) x T
    factor_returns = factor_returns.T
    residuals = x - factor_returns @ design.T
    total = np.var(x, axis=0, ddof=1) if n_rows > 1 else np.zeros(n_cols)
    loadings = design
    extra = max(0, min(int(residual_pcs), n_cols - 1, n_rows - 1))
    if extra:
        rcov = np.atleast_2d(np.cov(residuals, rowvar=False, ddof=1))
        _, pcs = _top_components(rcov, extra)
        pc_returns = residuals @ pcs
        residuals = residuals - pc_returns @ pcs.T
        factor_returns = np.column_stack([factor_returns, pc_returns])
        loadings = np.column_stack([design, pcs])
        labels = [*labels, *(f"pc{i + 1}" for i in range(extra))]
    factor_cov = (
        np.atleast_2d(np.cov(factor_returns, rowvar=False, ddof=1))
        if n_rows > 1
        else np.zeros((len(labels), len(labels)))
    )
    return FactorModel(names, tuple(labels), loadings, factor_cov, _specific(residuals, total))


def model_exposures(
    tickers: Sequence[str],
    returns: pd.DataFrame | np.ndarray,
    exposures: pd.DataFrame | None,
) -> pd.DataFrame:
    """Standardised exposures for ``tickers``: the columns of ``exposures``
    (rows tickers, raw values) where given, else momentum and volatility
    read from ``returns``. A ticker missing from ``exposures`` sits at the
    average (0) of every style and in no sector."""
    table = pd.DataFrame(np.asarray(returns, dtype=float), columns=list(tickers))
    derived = returns_style_exposures(table)
    if exposures is None or exposures.empty:
        raw = derived
    else:
        raw = exposures.reindex(list(tickers))
        for column in derived.columns:
            if column not in raw.columns or raw[column].isna().all():
                raw = raw.assign(**{column: derived[column]})
    return standardize_exposures(raw).reindex(list(tickers)).fillna(0.0)

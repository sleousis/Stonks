"""Covariance estimators behind one seam (BL-44).

A :class:`CovarianceEstimator` turns a table of per-bar returns (rows are
bars, oldest first, one column per ticker, no gaps) into a per-bar
covariance matrix. Every estimate goes through :func:`nearest_psd`, so the
result is symmetric and strictly positive definite even when the input is
singular (duplicate names, fewer rows than columns).

Estimators are found by name, like constructors:

- ``sample``: the usual sample covariance (ddof 1).
- ``ledoit_wolf``: Ledoit-Wolf shrinkage towards a scaled identity
  (scikit-learn, wrapped here).
- ``ewma``: zero-mean exponentially weighted covariance, weight
  ``lam**age`` normalised to 1 (RiskMetrics ``lam = 0.94``).
- ``denoised``: Marchenko-Pastur "constant residual" denoising of the
  correlation matrix (Lopez de Prado): eigenvalues under the random-matrix
  edge ``(1 + sqrt(N/T))**2`` are replaced by their average, then the
  sample volatilities are put back.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any, ClassVar

import numpy as np
import pandas as pd

#: Smallest eigenvalue :func:`nearest_psd` allows, relative to the mean variance.
PSD_FLOOR = 1e-10
_ABS_FLOOR = 1e-18


def nearest_psd(matrix: np.ndarray, floor: float = PSD_FLOOR) -> np.ndarray:
    """Symmetrise ``matrix`` and lift every eigenvalue to at least
    ``floor * mean(diagonal)``. A matrix that already clears the floor comes
    back unchanged (bar symmetrisation)."""
    m = np.asarray(matrix, dtype=float)
    m = 0.5 * (m + m.T)
    if m.size == 0:
        return m
    scale = float(np.mean(np.abs(np.diag(m))))
    minimum = max(floor * scale, _ABS_FLOOR)
    values, vectors = np.linalg.eigh(m)
    if values.min() >= minimum:
        return m
    fixed = (vectors * np.maximum(values, minimum)) @ vectors.T
    return 0.5 * (fixed + fixed.T)


def _as_array(returns: pd.DataFrame | np.ndarray) -> np.ndarray:
    x = np.asarray(returns, dtype=float)
    if x.ndim == 1:
        x = x[:, None]
    if x.shape[0] < 2:
        raise ValueError(f"a covariance estimate needs at least 2 rows, got {x.shape[0]}")
    if not np.all(np.isfinite(x)):
        raise ValueError("returns must be finite (drop gaps before estimating)")
    return x


class CovarianceEstimator(ABC):
    """Per-bar covariance of a return table. Subclasses implement
    :meth:`_estimate`; :meth:`estimate` validates and applies the PSD fix."""

    name: ClassVar[str] = ""

    def estimate(self, returns: pd.DataFrame | np.ndarray) -> np.ndarray:
        return nearest_psd(self._estimate(_as_array(returns)))

    @abstractmethod
    def _estimate(self, x: np.ndarray) -> np.ndarray: ...


_REGISTRY: dict[str, type[CovarianceEstimator]] = {}


def register_estimator(
    name: str,
) -> Callable[[type[CovarianceEstimator]], type[CovarianceEstimator]]:
    """Class decorator: register a :class:`CovarianceEstimator` under ``name``."""

    def decorate(cls: type[CovarianceEstimator]) -> type[CovarianceEstimator]:
        existing = _REGISTRY.get(name)
        if existing is not None and existing.__qualname__ != cls.__qualname__:
            raise ValueError(f"estimator {name!r} is already registered by {existing!r}")
        cls.name = name
        _REGISTRY[name] = cls
        return cls

    return decorate


def estimator_names() -> list[str]:
    return sorted(_REGISTRY)


def get_estimator(name: str, **kwargs: Any) -> CovarianceEstimator:
    """Build the estimator registered as ``name``. An unknown name raises
    ``ValueError`` listing the valid ones."""
    cls = _REGISTRY.get(name)
    if cls is None:
        raise ValueError(
            f"unknown covariance estimator {name!r}; choose one of {estimator_names()}"
        )
    return cls(**kwargs)


@register_estimator("sample")
class SampleCovariance(CovarianceEstimator):
    def _estimate(self, x: np.ndarray) -> np.ndarray:
        return np.atleast_2d(np.cov(x, rowvar=False, ddof=1))


@register_estimator("ledoit_wolf")
class LedoitWolfCovariance(CovarianceEstimator):
    """Ledoit-Wolf (2004) shrinkage; the scikit-learn type stays in here."""

    def _estimate(self, x: np.ndarray) -> np.ndarray:
        from sklearn.covariance import LedoitWolf

        return np.atleast_2d(LedoitWolf(assume_centered=False).fit(x).covariance_)


@register_estimator("ewma")
class EwmaCovariance(CovarianceEstimator):
    """Zero-mean EWMA covariance, ``sum_t w_t r_t r_t'`` with
    ``w_t ∝ lam**(T-1-t)`` normalised to sum to 1."""

    def __init__(self, lam: float = 0.94) -> None:
        if not 0.0 < lam < 1.0:
            raise ValueError(f"lam must be in (0, 1), got {lam}")
        self.lam = lam

    def _estimate(self, x: np.ndarray) -> np.ndarray:
        ages = np.arange(x.shape[0])[::-1]
        w = self.lam**ages
        w = w / w.sum()
        return (x * w[:, None]).T @ x


@register_estimator("denoised")
class DenoisedCovariance(CovarianceEstimator):
    """Constant-residual Marchenko-Pastur denoising of the correlation
    matrix, with the sample volatilities put back."""

    def _estimate(self, x: np.ndarray) -> np.ndarray:
        cov = np.atleast_2d(np.cov(x, rowvar=False, ddof=1))
        n_rows, n_cols = x.shape
        std = np.sqrt(np.clip(np.diag(cov), 0.0, None))
        safe = np.where(std > 0, std, 1.0)
        corr = cov / np.outer(safe, safe)
        np.fill_diagonal(corr, 1.0)
        values, vectors = np.linalg.eigh(corr)  # ascending
        edge = (1.0 + np.sqrt(n_cols / n_rows)) ** 2
        noise = values <= edge
        if noise.sum() > 1:
            values = values.copy()
            values[noise] = values[noise].mean()
            corr = (vectors * values) @ vectors.T
            d = np.sqrt(np.clip(np.diag(corr), _ABS_FLOOR, None))
            corr = corr / np.outer(d, d)
        return corr * np.outer(std, std)

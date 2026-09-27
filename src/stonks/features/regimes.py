"""Latent regime models (BL-46; Hamilton, Tsay, Dixon et al.).

A :class:`RegimeModel` learns hidden market states from a return series and
then says, bar by bar, how likely each state is.

- ``fit(returns)`` estimates the model on training data only.
- ``filtered_probs(returns)`` gives ``P(state | returns up to bar t)`` for
  every bar. These are *filtered* probabilities: the value at bar ``t``
  uses bars ``<= t`` only. Smoothed probabilities (Kim's smoother) use the
  whole sample, future included, and are never exposed here (P12).

:class:`MarkovSwitchingRegime` is a ``k``-state Markov-switching model
with a state-dependent mean and (by default) variance. It is fitted with
statsmodels' ``MarkovRegression``, wrapped here so no statsmodels type
leaves this module: the fit is reduced to plain numbers
(:class:`RegimeParams`: means, volatilities, transition matrix), states
are sorted by volatility (state 0 is the calmest, state ``k-1`` the most
volatile), and inference is our own numpy Hamilton filter
(:func:`hamilton_filter`) over those numbers.
"""

from __future__ import annotations

import math
import warnings
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

import numpy as np

__all__ = ["MarkovSwitchingRegime", "RegimeModel", "RegimeParams", "hamilton_filter"]

#: Fewest returns a fit accepts.
MIN_FIT_RETURNS = 50


@dataclass(frozen=True)
class RegimeParams:
    """A fitted regime model as plain numbers. ``transition[i, j]`` is
    ``P(state j at t | state i at t-1)``; rows sum to 1."""

    means: np.ndarray
    sigmas: np.ndarray
    transition: np.ndarray

    def __post_init__(self) -> None:
        k = len(self.means)
        if k < 2 or self.sigmas.shape != (k,) or self.transition.shape != (k, k):
            raise ValueError("regime params need k >= 2 means, k sigmas and a k x k matrix")
        if np.any(self.sigmas <= 0):
            raise ValueError("regime volatilities must be positive")
        if np.any(self.transition < 0) or not np.allclose(self.transition.sum(axis=1), 1.0):
            raise ValueError("each transition row must be a probability distribution")

    @property
    def k(self) -> int:
        return len(self.means)

    def stationary(self) -> np.ndarray:
        """The chain's stationary distribution (uniform if degenerate)."""
        k = self.k
        a = np.vstack([self.transition.T - np.eye(k), np.ones(k)])
        b = np.concatenate([np.zeros(k), [1.0]])
        pi, *_ = np.linalg.lstsq(a, b, rcond=None)
        pi = np.clip(pi, 0.0, None)
        total = pi.sum()
        return pi / total if total > 0 else np.full(k, 1.0 / k)

    def to_dict(self) -> dict[str, Any]:
        return {
            "means": [float(v) for v in self.means],
            "sigmas": [float(v) for v in self.sigmas],
            "transition": [[float(v) for v in row] for row in self.transition],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RegimeParams:
        return cls(
            means=np.asarray(data["means"], dtype=float),
            sigmas=np.asarray(data["sigmas"], dtype=float),
            transition=np.asarray(data["transition"], dtype=float),
        )


def hamilton_filter(returns: np.ndarray, params: RegimeParams) -> np.ndarray:
    """Filtered state probabilities, shape ``(n, k)``, starting from the
    stationary distribution. Row ``t`` uses ``returns[: t + 1]`` only.
    Non-finite returns carry the prediction forward without an update."""
    r = np.asarray(returns, dtype=float)
    xi = params.stationary()
    out = np.empty((len(r), params.k))
    log_norm = -0.5 * math.log(2 * math.pi) - np.log(params.sigmas)
    for t, value in enumerate(r):
        pred = params.transition.T @ xi
        if np.isfinite(value):
            z = (value - params.means) / params.sigmas
            loglik = log_norm - 0.5 * z * z
            weights = np.log(np.maximum(pred, 1e-300)) + loglik
            weights -= weights.max()  # log-sum-exp: safe against outliers
            post = np.exp(weights)
            xi = post / post.sum()
        else:
            xi = pred
        out[t] = xi
    return out


class RegimeModel(ABC):
    """A latent regime model (see the module doc)."""

    @abstractmethod
    def fit(self, returns: np.ndarray) -> RegimeModel: ...

    @abstractmethod
    def filtered_probs(self, returns: np.ndarray) -> np.ndarray:
        """``(n, k)`` filtered state probabilities."""

    @property
    @abstractmethod
    def high_vol_state(self) -> int:
        """Index of the most volatile state."""

    def high_vol_probability(self, returns: np.ndarray) -> float:
        """Filtered probability of the most volatile state at the last bar."""
        probs = self.filtered_probs(returns)
        return float(probs[-1, self.high_vol_state]) if len(probs) else float("nan")


class MarkovSwitchingRegime(RegimeModel):
    """``k``-state Markov-switching mean (and variance) model."""

    kind = "markov_switching"

    def __init__(self, k: int = 2, switching_variance: bool = True) -> None:
        if k < 2:
            raise ValueError(f"k must be >= 2, got {k}")
        self.k = int(k)
        self.switching_variance = bool(switching_variance)
        self.params: RegimeParams | None = None

    def fit(self, returns: np.ndarray) -> MarkovSwitchingRegime:
        from statsmodels.tsa.regime_switching.markov_regression import MarkovRegression

        r = np.asarray(returns, dtype=float)
        r = r[np.isfinite(r)]
        if len(r) < MIN_FIT_RETURNS:
            raise ValueError(f"need at least {MIN_FIT_RETURNS} returns, got {len(r)}")
        model = MarkovRegression(
            r, k_regimes=self.k, trend="c", switching_variance=self.switching_variance
        )
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # convergence chatter from the optimiser
            result: Any = model.fit(disp=False)
        values = dict(zip(model.param_names, np.asarray(result.params, dtype=float), strict=True))
        means = np.array([values[f"const[{i}]"] for i in range(self.k)])
        if self.switching_variance:
            variances = np.array([values[f"sigma2[{i}]"] for i in range(self.k)])
        else:
            variances = np.full(self.k, values["sigma2"])
        # statsmodels: matrix[i, j] = P(s_t = i | s_{t-1} = j), so transpose
        transition = np.asarray(model.regime_transition_matrix(result.params))[:, :, 0].T
        transition = np.clip(transition, 0.0, 1.0)
        transition /= transition.sum(axis=1, keepdims=True)
        order = np.argsort(variances, kind="stable")
        self.params = RegimeParams(
            means=means[order],
            sigmas=np.sqrt(np.maximum(variances[order], 1e-18)),
            transition=transition[np.ix_(order, order)],
        )
        return self

    def filtered_probs(self, returns: np.ndarray) -> np.ndarray:
        if self.params is None:
            raise RuntimeError("regime model is not fitted")
        return hamilton_filter(returns, self.params)

    @property
    def high_vol_state(self) -> int:
        return self.k - 1

    def to_dict(self) -> dict[str, Any]:
        if self.params is None:
            raise RuntimeError("regime model is not fitted")
        return {
            "kind": self.kind,
            "k": self.k,
            "switching_variance": self.switching_variance,
            **self.params.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MarkovSwitchingRegime:
        model = cls(k=int(data["k"]), switching_variance=bool(data["switching_variance"]))
        model.params = RegimeParams.from_dict(data)
        return model

"""Volatility forecasters behind one seam (BL-48; Tsay, Danielsson, Gatheral).

A :class:`VolForecaster` learns from a series of per-bar returns and
forecasts the per-bar volatility ahead:

- ``fit(returns)`` estimates the model (returns in plain units, 0.01 = 1%);
- ``conditional_vol()`` is the in-sample volatility of each bar, known
  before that bar (a one-step-ahead forecast, so no look-ahead);
- ``forecast(horizon)`` is the per-bar volatility over the next
  ``horizon`` bars (the square root of their mean variance).

Implementations (:data:`VOL_FORECASTERS`):

- ``ewma``: RiskMetrics, ``var_t = lam * var_{t-1} + (1 - lam) * r_{t-1}^2``
  with ``lam = 0.94``.
- ``garch``: GARCH(1,1) with Student-t shocks, fitted by the ``arch``
  library and wrapped here. Only plain floats leave the fit (``mu``,
  ``omega``, ``alpha``, ``beta``, ``nu``); forecasts and simulations are
  our own recursions over them. :meth:`GarchVol.simulate` runs the fitted
  recursion over given standardised shocks, which is what filtered
  historical simulation (the ``stress`` test) needs.
- ``har_rv``: Corsi's heterogeneous autoregression on the squared-return
  proxy of realised variance, with daily, weekly (5) and monthly (22)
  components, by least squares.

No seed reads the future: EWMA starts from the first squared return and
HAR's first month from the squared returns so far. Model parameters
(GARCH, HAR) are fitted on the whole sample given to ``fit``, so
``conditional_vol`` is an in-sample path; fit on data up to the decision
for an out-of-sample forecast.
"""

from __future__ import annotations

import math
import warnings
from abc import ABC, abstractmethod
from typing import Any, ClassVar, Literal

import numpy as np

__all__ = [
    "VOL_FORECASTERS",
    "EwmaVol",
    "GarchVol",
    "HarRv",
    "VolForecaster",
    "build_vol_forecaster",
]

#: Fewest returns any forecaster fits on.
MIN_RETURNS = 30
#: ``arch`` fits best on returns in percent.
_SCALE = 100.0


def _clean(returns: Any, minimum: int = MIN_RETURNS) -> np.ndarray:
    r = np.asarray(returns, dtype=float)
    r = r[np.isfinite(r)]
    if len(r) < minimum:
        raise ValueError(f"need at least {minimum} finite returns, got {len(r)}")
    return r


class VolForecaster(ABC):
    """Per-bar volatility forecaster (see the module doc)."""

    name: ClassVar[str] = ""

    @abstractmethod
    def fit(self, returns: np.ndarray) -> VolForecaster: ...

    @abstractmethod
    def conditional_vol(self) -> np.ndarray:
        """In-sample one-step-ahead volatility, one value per fitted return."""

    @abstractmethod
    def forecast(self, horizon: int = 1) -> float:
        """Per-bar volatility over the next ``horizon`` bars."""

    @staticmethod
    def _check_horizon(horizon: int) -> None:
        if horizon < 1:
            raise ValueError(f"horizon must be >= 1, got {horizon}")


class EwmaVol(VolForecaster):
    name: ClassVar[str] = "ewma"

    def __init__(self, lam: float = 0.94) -> None:
        if not 0.0 < lam < 1.0:
            raise ValueError(f"lam must lie in (0, 1), got {lam}")
        self.lam = float(lam)
        self._var: np.ndarray | None = None
        self._next: float | None = None

    def fit(self, returns: np.ndarray) -> EwmaVol:
        r = _clean(returns, 2)
        var = np.empty(len(r))
        # Seed with the first squared return: a warm-up value, not a
        # forecast, but it reads nothing past the first bar (BE-59).
        var[0] = float(r[0] ** 2)
        for t in range(1, len(r)):
            var[t] = self.lam * var[t - 1] + (1.0 - self.lam) * r[t - 1] ** 2
        self._var = var
        self._next = self.lam * var[-1] + (1.0 - self.lam) * r[-1] ** 2
        return self

    def conditional_vol(self) -> np.ndarray:
        if self._var is None:
            raise RuntimeError("forecaster is not fitted")
        return np.sqrt(self._var)

    def forecast(self, horizon: int = 1) -> float:
        self._check_horizon(horizon)
        if self._next is None:
            raise RuntimeError("forecaster is not fitted")
        return math.sqrt(self._next)  # EWMA variance is flat ahead


class GarchVol(VolForecaster):
    """GARCH(1,1) with a constant mean (see the module doc)."""

    name: ClassVar[str] = "garch"

    def __init__(self, dist: Literal["t", "normal"] = "t") -> None:
        if dist not in ("t", "normal"):
            raise ValueError(f"dist must be 't' or 'normal', got {dist!r}")
        self.dist: Literal["t", "normal"] = dist
        self.mu = self.omega = self.alpha = self.beta = math.nan
        self.nu: float | None = None
        self._returns: np.ndarray | None = None
        self._var: np.ndarray | None = None

    @property
    def is_fitted(self) -> bool:
        return self._var is not None

    def fit(self, returns: np.ndarray) -> GarchVol:
        from arch import arch_model

        r = _clean(returns)
        model = arch_model(
            r * _SCALE, mean="Constant", vol="GARCH", p=1, q=1, dist=self.dist, rescale=False
        )
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            result = model.fit(disp="off")
        p = {str(k): float(v) for k, v in result.params.items()}
        self.mu = p["mu"] / _SCALE
        self.omega = p["omega"] / _SCALE**2
        self.alpha = p["alpha[1]"]
        self.beta = p["beta[1]"]
        self.nu = p.get("nu") if self.dist == "t" else None
        self._returns = r
        self._var = self._filter(r)
        return self

    def _filter(self, r: np.ndarray) -> np.ndarray:
        eps = r - self.mu
        var = np.empty(len(r))
        var[0] = self.unconditional_variance()
        if not np.isfinite(var[0]):
            var[0] = float(np.var(r))
        for t in range(1, len(r)):
            var[t] = self.omega + self.alpha * eps[t - 1] ** 2 + self.beta * var[t - 1]
        return var

    def unconditional_variance(self) -> float:
        persistence = self.alpha + self.beta
        return self.omega / (1.0 - persistence) if persistence < 1.0 else math.inf

    def _require(self) -> tuple[np.ndarray, np.ndarray]:
        if self._returns is None or self._var is None:
            raise RuntimeError("forecaster is not fitted")
        return self._returns, self._var

    def conditional_vol(self) -> np.ndarray:
        return np.sqrt(self._require()[1])

    def next_variance(self) -> float:
        r, var = self._require()
        return self.omega + self.alpha * (r[-1] - self.mu) ** 2 + self.beta * var[-1]

    def forecast(self, horizon: int = 1) -> float:
        self._check_horizon(horizon)
        v = self.next_variance()
        total = 0.0
        for _ in range(horizon):
            total += v
            v = self.omega + (self.alpha + self.beta) * v
        return math.sqrt(total / horizon)

    def standardized_residuals(self) -> np.ndarray:
        r, var = self._require()
        return (r - self.mu) / np.sqrt(var)

    def standardize(self, returns: np.ndarray) -> np.ndarray:
        """``returns`` run through the fitted recursion and divided by its
        conditional volatility (the shocks that drove them)."""
        self._require()
        r = np.asarray(returns, dtype=float)
        return (r - self.mu) / np.sqrt(self._filter(r))

    def simulate(self, shocks: np.ndarray) -> np.ndarray:
        """Returns of the fitted recursion driven by standardised ``shocks``
        (one per bar), starting from the variance after the last fitted
        bar."""
        v = self.next_variance()
        out = np.empty(len(shocks))
        for t, z in enumerate(np.asarray(shocks, dtype=float)):
            eps = math.sqrt(v) * z
            out[t] = self.mu + eps
            v = self.omega + self.alpha * eps * eps + self.beta * v
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "dist": self.dist,
            "mu": self.mu,
            "omega": self.omega,
            "alpha": self.alpha,
            "beta": self.beta,
            "nu": self.nu,
        }


class HarRv(VolForecaster):
    """HAR on squared returns (see the module doc)."""

    name: ClassVar[str] = "har_rv"
    LAGS: ClassVar[tuple[int, int, int]] = (1, 5, 22)

    def __init__(self) -> None:
        self.coef: np.ndarray | None = None
        self._rv: np.ndarray | None = None

    def _design(self, rv: np.ndarray) -> np.ndarray:
        """Rows ``[1, rv_t, mean(rv, 5), mean(rv, 22)]`` for every ``t``
        with a full month behind it (``t >= 21``)."""
        cum = np.concatenate([[0.0], np.cumsum(rv)])
        t = np.arange(self.LAGS[2] - 1, len(rv))
        cols = [np.ones(len(t))]
        for lag in self.LAGS:
            cols.append((cum[t + 1] - cum[t + 1 - lag]) / lag)
        return np.column_stack(cols)

    def fit(self, returns: np.ndarray) -> HarRv:
        r = _clean(returns, self.LAGS[2] + 10)
        rv = r**2
        x = self._design(rv)
        coef, *_ = np.linalg.lstsq(x[:-1], rv[self.LAGS[2] :], rcond=None)
        self.coef = coef
        self._rv = rv
        return self

    def _require(self) -> tuple[np.ndarray, np.ndarray]:
        if self.coef is None or self._rv is None:
            raise RuntimeError("forecaster is not fitted")
        return self.coef, self._rv

    def conditional_vol(self) -> np.ndarray:
        coef, rv = self._require()
        fitted = self._design(rv)[:-1] @ coef
        # Warm-up bars before a full month: the mean of the squared returns
        # so far (the first bar seeds itself), never a later one (BE-59).
        n = self.LAGS[2]
        seen = np.cumsum(rv[:n])
        head = np.concatenate([[rv[0]], seen[:-1] / np.arange(1, n)])
        return np.sqrt(np.maximum(np.concatenate([head, fitted]), 0.0))

    def forecast(self, horizon: int = 1) -> float:
        self._check_horizon(horizon)
        coef, rv = self._require()
        path = list(rv[-self.LAGS[2] :])
        total = 0.0
        for _ in range(horizon):
            window = np.asarray(path)
            nxt = float(
                coef[0]
                + coef[1] * window[-1]
                + coef[2] * window[-5:].mean()
                + coef[3] * window.mean()
            )
            nxt = max(nxt, 0.0)
            total += nxt
            path = path[1:] + [nxt]
        return math.sqrt(total / horizon)


#: ``name -> class`` of every forecaster.
VOL_FORECASTERS: dict[str, type[VolForecaster]] = {
    cls.name: cls for cls in (EwmaVol, GarchVol, HarRv)
}


def build_vol_forecaster(name: str, **options: Any) -> VolForecaster:
    cls = VOL_FORECASTERS.get(name)
    if cls is None:
        raise ValueError(f"unknown vol forecaster {name!r}; choose from {sorted(VOL_FORECASTERS)}")
    return cls(**options)

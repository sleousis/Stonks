"""Shared plumbing for the covariance-based constructors (BL-44).

- :class:`RiskBasedSettings`: the knobs ``hrp``, ``erc`` and
  ``mean_variance_costs`` share (candidate count, weight cap, covariance
  estimator and window).
- :func:`covariance_for`: an annualised covariance for a set of tickers,
  built from ``returns_history`` rows up to ``as_of`` where there is enough
  history, and from ``vols_annual`` (zero correlation) where there is not.
- :func:`cap_weights`: scale weights to a total and cap each one,
  handing the excess to the names under the cap.
- :class:`RiskBasedConstructor`: picks the best ``top_n`` scores, builds
  the covariance, asks the subclass for weights summing to 1, caps them
  and reports the effective number of bets.

A private module, so constructor discovery skips it.
"""

from __future__ import annotations

import math
from abc import abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

import numpy as np
import pandas as pd
from pydantic import Field, field_validator, model_validator

from stonks.portfolio.base import (
    ConstructionInput,
    ConstructorSettings,
    PortfolioConstructor,
    TargetBook,
    Ticker,
    combine_signals,
)
from stonks.portfolio.covariance import estimator_names, get_estimator, nearest_psd
from stonks.portfolio.diversification import effective_number_of_bets


class RiskBasedSettings(ConstructorSettings):
    top_n: int = Field(default=20, ge=1)
    max_weight: float = Field(default=1.0, gt=0.0, le=1.0)
    estimator: str = "ledoit_wolf"
    #: Knobs of the estimator (``n_factors`` for ``pca``, ``residual_pcs``
    #: for ``style``, ``lam`` for ``ewma``).
    estimator_params: dict[str, Any] = Field(default_factory=dict)
    lookback: int = Field(default=252, ge=2)
    min_observations: int = Field(default=60, ge=2)
    periods_per_year: float = Field(default=252.0, gt=0.0)

    @field_validator("estimator")
    @classmethod
    def _known_estimator(cls, value: str) -> str:
        if value not in estimator_names():
            raise ValueError(
                f"unknown covariance estimator {value!r}; choose one of {estimator_names()}"
            )
        return value

    @model_validator(mode="after")
    def _estimator_builds(self) -> RiskBasedSettings:
        try:
            get_estimator(self.estimator, **self.estimator_params)
        except TypeError as exc:
            raise ValueError(f"bad estimator_params for {self.estimator!r}: {exc}") from None
        return self


@dataclass(frozen=True)
class CovarianceView:
    """An annualised covariance over ``tickers`` (sorted) and where it
    came from: ``history``, ``vols`` or ``mixed``."""

    tickers: list[Ticker]
    matrix: np.ndarray
    source: str
    observations: int

    @property
    def vols(self) -> np.ndarray:
        return np.sqrt(np.diag(self.matrix))


def _history_until(history: pd.DataFrame | None, as_of: date) -> pd.DataFrame | None:
    """Rows dated on or before ``as_of`` (P1: no look-ahead). An index that
    isn't dates is trusted to be causal already."""
    if history is None or history.empty:
        return None
    try:
        index = pd.DatetimeIndex(pd.to_datetime(history.index))
    except (TypeError, ValueError):
        return history
    if index.tz is not None:
        index = index.tz_localize(None)
    return history.loc[index.normalize() <= pd.Timestamp(as_of)]


def covariance_for(
    inp: ConstructionInput,
    tickers: Sequence[Ticker],
    settings: RiskBasedSettings,
) -> CovarianceView:
    """Annualised covariance for ``tickers``. Names with at least
    ``min_observations`` joint rows of history (last ``lookback`` rows up to
    ``as_of``) get the configured estimator; the rest get their
    ``vols_annual`` on the diagonal and no correlation. Names with neither
    are left out."""
    ordered = sorted(set(tickers))
    history = _history_until(inp.returns_history, inp.as_of)
    hist_cols: list[Ticker] = []
    block = None
    if history is not None:
        recent = history.tail(settings.lookback)
        hist_cols = [
            t
            for t in ordered
            if t in recent.columns
            and recent[t].notna().sum() >= settings.min_observations
            and np.isfinite(recent[t].dropna().astype(float)).all()
        ]
        if hist_cols:
            block = recent[hist_cols].astype(float).dropna()
            if len(block) < settings.min_observations:
                hist_cols, block = [], None
    kept_vol = [t for t in ordered if t not in hist_cols and inp.vol(t) is not None]
    kept = sorted(hist_cols + kept_vol)
    n = len(kept)
    matrix = np.zeros((n, n))
    position = {t: i for i, t in enumerate(kept)}
    observations = 0
    if hist_cols and block is not None:
        estimator = get_estimator(settings.estimator, **settings.estimator_params)
        estimate = estimator.estimate(block, exposures=inp.factor_exposures)
        estimate = estimate * settings.periods_per_year
        idx = [position[t] for t in hist_cols]
        matrix[np.ix_(idx, idx)] = estimate
        observations = len(block)
    for t in kept_vol:
        i = position[t]
        matrix[i, i] = inp.vol(t) ** 2  # type: ignore[operator]
    if hist_cols and kept_vol:
        source = "mixed"
    elif hist_cols:
        source = "history"
    else:
        source = "vols"
    if n:
        matrix = nearest_psd(matrix)
    return CovarianceView(tickers=kept, matrix=matrix, source=source, observations=observations)


def cap_weights(raw: np.ndarray, cap: float, total: float = 1.0) -> np.ndarray:
    """Scale non-negative ``raw`` to sum to ``total``, then cap each weight
    at ``cap`` and hand the excess to the uncapped names pro rata. When
    every name is at the cap the book sums to less than ``total`` (cash)."""
    w = np.clip(np.asarray(raw, dtype=float), 0.0, None)
    if w.size == 0 or w.sum() <= 0:
        return np.zeros_like(w)
    w = w / w.sum() * total
    capped = np.zeros(w.size, dtype=bool)
    for _ in range(w.size + 1):
        over = (w > cap + 1e-15) & ~capped
        if not over.any():
            break
        capped |= over
        w[capped] = cap
        free = ~capped
        room = total - cap * capped.sum()
        if not free.any() or room <= 0 or w[free].sum() <= 0:
            break
        w[free] = w[free] / w[free].sum() * room
    return np.minimum(w, cap)


def top_candidates(inp: ConstructionInput, n: int) -> tuple[list[Ticker], dict, dict]:
    """The ``n`` best positive combined scores among tradable tickers (ties
    by ticker), the combined scores and their attribution."""
    combined, attribution = combine_signals(inp)
    eligible = [t for t, v in combined.items() if v > 0 and inp.tradable(t)]
    chosen = sorted(eligible, key=lambda t: (-combined[t], t))[:n]
    return chosen, combined, attribution


class RiskBasedConstructor(PortfolioConstructor):
    """Base for constructors whose weights come from a covariance matrix.
    Subclasses implement :meth:`raw_weights` (non-negative, any scale)."""

    Settings = RiskBasedSettings

    def returns_lookback(self) -> int | None:
        return self.settings.lookback  # type: ignore[attr-defined]

    @abstractmethod
    def raw_weights(self, cov: CovarianceView, scores: np.ndarray) -> np.ndarray: ...

    def target_weights(self, inp: ConstructionInput) -> TargetBook:
        s: RiskBasedSettings = self.settings  # type: ignore[assignment]
        chosen, combined, attribution = top_candidates(inp, s.top_n)
        cov = covariance_for(inp, chosen, s)
        meta: dict = {"covariance": cov.source, "observations": cov.observations}
        if not cov.tickers:
            meta["enb"] = 0.0
            return self.finalize({}, meta=meta)
        scores = np.array([combined[t] for t in cov.tickers], dtype=float)
        raw = self.raw_weights(cov, scores)
        w = cap_weights(raw, s.max_weight, s.max_gross)
        meta["enb"] = effective_number_of_bets(w, cov.matrix)
        weights: Mapping[Ticker, float] = {
            t: float(x) for t, x in zip(cov.tickers, w, strict=True) if x > 0 and math.isfinite(x)
        }
        return self.finalize(weights, attribution, meta)

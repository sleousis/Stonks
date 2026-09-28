"""Calibration of probability forecasts: Brier score and reliability.

A classifier that says 70% should be right about 70% of the time. For
``n`` forecasts ``p_i`` with outcomes ``y_i`` in ``{0, 1}``:

- :func:`brier_score`: ``mean((p - y)^2)``, 0 is perfect, and always
  saying the base rate scores ``rate * (1 - rate)``;
- :func:`reliability`: forecasts grouped into equal-width probability bins,
  each bin's mean forecast against how often the event happened;
- :func:`expected_calibration_error`: the count-weighted mean gap between
  the two across bins.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

__all__ = [
    "ReliabilityBin",
    "brier_score",
    "expected_calibration_error",
    "reliability",
]


@dataclass(frozen=True)
class ReliabilityBin:
    lower: float
    upper: float
    count: int
    #: Mean forecast in the bin (``None`` for an empty bin).
    mean_forecast: float | None
    #: Share of the bin's events that happened (``None`` for an empty bin).
    observed_rate: float | None


def _pair(p: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    probs = np.asarray(p, dtype=float).ravel()
    outcomes = np.asarray(y, dtype=float).ravel()
    if probs.shape != outcomes.shape:
        raise ValueError(f"{probs.size} forecasts but {outcomes.size} outcomes")
    if probs.size and (np.any(probs < 0) or np.any(probs > 1)):
        raise ValueError("forecasts must lie in [0, 1]")
    if outcomes.size and not np.all(np.isin(outcomes, (0.0, 1.0))):
        raise ValueError("outcomes must be 0 or 1")
    return probs, outcomes


def brier_score(p: np.ndarray, y: np.ndarray) -> float:
    """``mean((p - y)^2)``. ``ValueError`` with no forecasts."""
    probs, outcomes = _pair(p, y)
    if probs.size == 0:
        raise ValueError("need at least one forecast")
    return float(np.mean((probs - outcomes) ** 2))


def reliability(p: np.ndarray, y: np.ndarray, n_bins: int = 10) -> list[ReliabilityBin]:
    """``n_bins`` equal-width bins over ``[0, 1]``; a forecast of exactly 1
    falls in the last bin."""
    if n_bins < 1:
        raise ValueError(f"n_bins must be >= 1, got {n_bins}")
    probs, outcomes = _pair(p, y)
    edges = np.linspace(0.0, 1.0, n_bins + 1)
    which = np.minimum((probs * n_bins).astype(int), n_bins - 1)
    out = []
    for b in range(n_bins):
        mask = which == b
        count = int(mask.sum())
        out.append(
            ReliabilityBin(
                lower=float(edges[b]),
                upper=float(edges[b + 1]),
                count=count,
                mean_forecast=float(probs[mask].mean()) if count else None,
                observed_rate=float(outcomes[mask].mean()) if count else None,
            )
        )
    return out


def expected_calibration_error(bins: list[ReliabilityBin]) -> float | None:
    """Count-weighted mean ``|mean_forecast - observed_rate|``; ``None``
    when every bin is empty."""
    total = sum(b.count for b in bins)
    if total == 0:
        return None
    return float(
        sum(
            b.count * abs((b.mean_forecast or 0.0) - (b.observed_rate or 0.0))
            for b in bins
            if b.count
        )
        / total
    )

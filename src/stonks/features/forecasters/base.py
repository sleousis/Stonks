"""The forecaster seam (roadmap 23.11).

A :class:`Forecaster` reads a context of bars (oldest first, at least a
``close`` column, and ``open``, ``high``, ``low``, ``volume`` for models
that read OHLCV) and returns a :class:`Forecast` of the next ``horizon``
closes: quantiles at fixed levels, plus sample paths when the model draws
them.

Pretrained models remember the series they were trained on, so each one
declares a date after which its evidence is clean (:meth:`Forecaster.cutoff`):
the published end of its pretraining data, else the release date of its
weights. The lab refuses a validation window that starts on or before it
(``lab/preflight.py``), the same rule as the AI research loop's model
cutoff. Statistical baselines learn only from the context, so they have no
cutoff.

Adapters import their model package inside their methods, never at module
import, so the default install and CI never need torch.
"""

from __future__ import annotations

import importlib.util
import math
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any, ClassVar

import numpy as np
import pandas as pd

__all__ = ["DEFAULT_LEVELS", "MIN_CONTEXT", "Forecast", "Forecaster", "context_closes"]

#: Quantile levels a forecast carries unless the caller asks for others.
DEFAULT_LEVELS: tuple[float, ...] = (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9)
#: Fewest context bars any forecaster accepts.
MIN_CONTEXT = 16


def _check_levels(levels: Sequence[float]) -> tuple[float, ...]:
    out = tuple(float(x) for x in levels)
    if not out or any(not 0.0 < x < 1.0 for x in out) or list(out) != sorted(set(out)):
        raise ValueError(f"levels must be distinct, increasing and inside (0, 1), got {levels}")
    if not any(math.isclose(x, 0.5) for x in out):
        raise ValueError("levels must include the median 0.5")
    return out


@dataclass(frozen=True)
class Forecast:
    """Forecast closes for the next ``horizon`` bars.

    ``quantiles[i, k]`` is the ``levels[i]`` quantile of the close ``k + 1``
    bars ahead. ``samples`` (``n_paths x horizon``) is set when the model
    draws paths. Crossed quantiles are sorted on construction."""

    last: float
    levels: tuple[float, ...]
    quantiles: np.ndarray
    samples: np.ndarray | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        levels = _check_levels(self.levels)
        q = np.asarray(self.quantiles, dtype=float)
        if q.ndim != 2 or q.shape[0] != len(levels) or q.shape[1] < 1:
            raise ValueError(
                f"quantiles must have shape (len(levels), horizon), got {q.shape} "
                f"for {len(levels)} levels"
            )
        object.__setattr__(self, "levels", levels)
        object.__setattr__(self, "quantiles", np.sort(q, axis=0))
        if self.samples is not None:
            object.__setattr__(self, "samples", np.asarray(self.samples, dtype=float))

    @classmethod
    def from_samples(
        cls, last: float, samples: Any, levels: Sequence[float] = DEFAULT_LEVELS
    ) -> Forecast:
        paths = np.asarray(samples, dtype=float)
        if paths.ndim != 2:
            raise ValueError(f"samples must be n_paths x horizon, got shape {paths.shape}")
        lv = _check_levels(levels)
        return cls(float(last), lv, np.quantile(paths, lv, axis=0), paths)

    @property
    def horizon(self) -> int:
        return int(self.quantiles.shape[1])

    def median(self) -> np.ndarray:
        """The median close at each step ahead."""
        i = next(i for i, x in enumerate(self.levels) if math.isclose(x, 0.5))
        return self.quantiles[i].copy()

    def point(self) -> float:
        """The median close ``horizon`` bars ahead."""
        return float(self.median()[-1])

    def log_return(self) -> float:
        """``ln(point / last)``: the forecast return over the horizon."""
        return math.log(self.point() / self.last)

    def quantile_returns(self) -> np.ndarray:
        """``ln(q / last)`` at each level, ``horizon`` bars ahead."""
        return np.log(self.quantiles[:, -1] / self.last)

    def prob_up(self) -> float:
        """P(close ``horizon`` bars ahead > last). From the paths when there
        are any, else by linear interpolation of the quantiles, which caps
        it at the outer levels (0.1 to 0.9 by default)."""
        if self.samples is not None and self.samples.size:
            return float(np.mean(self.samples[:, -1] > self.last))
        q = self.quantiles[:, -1]
        if np.allclose(q, self.last):
            return 0.5
        return float(1.0 - np.interp(self.last, q, self.levels))


def context_closes(context: pd.DataFrame, minimum: int = MIN_CONTEXT) -> np.ndarray:
    """The context's closes as floats, checked: finite, positive, enough."""
    if "close" not in context.columns:
        raise ValueError("the context needs a close column")
    closes = context["close"].to_numpy(dtype=float)
    if len(closes) < minimum:
        raise ValueError(f"need at least {minimum} context bars, got {len(closes)}")
    if not np.all(np.isfinite(closes)) or np.any(closes <= 0):
        raise ValueError("context closes must be finite and positive")
    return closes


class Forecaster(ABC):
    """Forecasts the next closes of each context (see the module doc)."""

    #: Registry key.
    name: ClassVar[str] = ""
    #: One line for listings and docs.
    description: ClassVar[str] = ""
    #: SPDX id of the weights' licence ("" for baselines with no weights).
    licence: ClassVar[str] = ""
    #: True for models trained on outside data (weights).
    pretrained: ClassVar[bool] = False
    #: The published end of the pretraining data, when there is one.
    pretrain_cutoff: ClassVar[date | None] = None
    #: The release date of the weights (the cutoff when none is published).
    release_date: ClassVar[date | None] = None
    #: Import names the model needs (checked by :meth:`is_available`).
    requires: ClassVar[tuple[str, ...]] = ()
    #: The optional dependency group that installs them (``uv sync --extra``).
    extra: ClassVar[str | None] = None
    #: True when the model reads open, high, low and volume as well.
    reads_ohlcv: ClassVar[bool] = False
    #: Fewest context bars the model accepts.
    min_context: ClassVar[int] = MIN_CONTEXT

    @classmethod
    def cutoff(cls) -> date | None:
        """The last day the model may have seen: the published pretraining
        cutoff, else the weights' release date. ``None`` for baselines."""
        if not cls.pretrained:
            return None
        return cls.pretrain_cutoff or cls.release_date

    @classmethod
    def is_available(cls) -> bool:
        """True when every package in :attr:`requires` can be imported."""
        return all(importlib.util.find_spec(m) is not None for m in cls.requires)

    @abstractmethod
    def predict_batch(
        self,
        contexts: Sequence[pd.DataFrame],
        horizon: int,
        levels: Sequence[float] = DEFAULT_LEVELS,
    ) -> list[Forecast]:
        """One forecast per context, in order."""

    def predict(
        self, context: pd.DataFrame, horizon: int, levels: Sequence[float] = DEFAULT_LEVELS
    ) -> Forecast:
        return self.predict_batch([context], horizon, levels)[0]

    @staticmethod
    def _check_horizon(horizon: int) -> int:
        if int(horizon) < 1:
            raise ValueError(f"horizon must be >= 1, got {horizon}")
        return int(horizon)

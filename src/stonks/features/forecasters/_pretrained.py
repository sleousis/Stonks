"""Shared plumbing of the pretrained adapters: options, lazy loading and a
clear message when the optional group is missing."""

from __future__ import annotations

import importlib.util
from collections.abc import Sequence
from typing import Any, ClassVar

import numpy as np
import pandas as pd

from stonks.features.forecasters.base import Forecaster, context_closes


class ModelUnavailableError(ImportError):
    """The model's optional packages (or its code) are not installed."""


class PretrainedForecaster(Forecaster):
    """A model with weights, loaded on first use.

    ``model_id`` is a Hugging Face id or a local folder. ``local_files_only``
    forbids downloads (weights must already be in the cache or the folder);
    tests always set it. ``max_context`` caps the bars handed to the model."""

    pretrained: ClassVar[bool] = True
    default_model_id: ClassVar[str] = ""
    default_max_context: ClassVar[int] = 512

    def __init__(
        self,
        model_id: str | None = None,
        *,
        device: str = "cpu",
        local_files_only: bool = False,
        max_context: int | None = None,
    ) -> None:
        self.model_id = model_id or self.default_model_id
        self.device = device
        self.local_files_only = bool(local_files_only)
        self.max_context = int(max_context or self.default_max_context)
        if self.max_context < self.min_context:
            raise ValueError(f"max_context must be >= {self.min_context}")
        self._model: Any = None

    @classmethod
    def missing(cls) -> list[str]:
        return [m for m in cls.requires if importlib.util.find_spec(m) is None]

    def _require(self) -> None:
        gone = self.missing()
        if gone:
            raise ModelUnavailableError(
                f"forecaster {self.name!r} needs {', '.join(gone)}: install the optional "
                f"group with `uv sync --extra {self.extra}`"
            )

    def model(self) -> Any:
        """The loaded model (loads it once)."""
        if self._model is None:
            self._require()
            self._model = self._load()
        return self._model

    def _load(self) -> Any:
        raise NotImplementedError

    def _closes(self, contexts: Sequence[pd.DataFrame]) -> list[np.ndarray]:
        return [context_closes(c, self.min_context)[-self.max_context :] for c in contexts]


def interpolate_levels(
    source_levels: Sequence[float], values: np.ndarray, levels: Sequence[float]
) -> np.ndarray:
    """Quantiles at ``levels`` from quantiles at ``source_levels``.

    ``values`` is ``len(source_levels) x horizon``. Levels outside the
    source range take the nearest source quantile."""
    src = np.asarray(source_levels, dtype=float)
    v = np.sort(np.asarray(values, dtype=float), axis=0)
    return np.stack(
        [np.array([np.interp(x, src, v[:, k]) for x in levels]) for k in range(v.shape[1])],
        axis=1,
    )

"""Chronos adapters (Amazon, Apache-2.0 code and weights).

Install with ``uv sync --extra chronos`` (the ``chronos-forecasting``
package and torch). Chronos reads one series, so only closes go in; it
scales each series itself and returns quantiles in the input's units.

- ``chronos_bolt``: Chronos-Bolt small (48M), patch based and fast on a
  CPU. Weights released 2024-11-25.
- ``chronos_2``: Chronos-2 (120M). Weights released 2025-10-30.

Neither model card publishes the end date of the pretraining data, so the
cutoff is the release date.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import Any, ClassVar

import numpy as np
import pandas as pd

from stonks.features.forecasters._pretrained import PretrainedForecaster
from stonks.features.forecasters.base import DEFAULT_LEVELS, Forecast, _check_levels

__all__ = ["Chronos2Forecaster", "ChronosBoltForecaster"]


def _as_horizon_by_level(raw: Any, horizon: int, n_levels: int) -> np.ndarray:
    """One series' quantiles as ``horizon x n_levels`` whatever the batch
    and variate axes the pipeline returned."""
    arr = np.asarray(raw.detach().cpu().numpy() if hasattr(raw, "detach") else raw, dtype=float)
    return arr.reshape(-1, horizon, n_levels)[0]


class _Chronos(PretrainedForecaster):
    licence: ClassVar[str] = "Apache-2.0"
    requires: ClassVar[tuple[str, ...]] = ("chronos", "torch")
    extra: ClassVar[str | None] = "chronos"

    def _load(self) -> Any:
        from chronos import BaseChronosPipeline  # pyright: ignore[reportMissingImports]

        return BaseChronosPipeline.from_pretrained(
            self.model_id, device_map=self.device, local_files_only=self.local_files_only
        )

    def predict_batch(
        self,
        contexts: Sequence[pd.DataFrame],
        horizon: int,
        levels: Sequence[float] = DEFAULT_LEVELS,
    ) -> list[Forecast]:
        h = self._check_horizon(horizon)
        lv = _check_levels(levels)
        closes = self._closes(contexts)
        pipeline = self.model()  # checks the optional group first
        import torch  # pyright: ignore[reportMissingImports]

        quantiles, _mean = pipeline.predict_quantiles(
            [torch.tensor(c, dtype=torch.float32) for c in closes],
            prediction_length=h,
            quantile_levels=list(lv),
        )
        out = []
        for i, c in enumerate(closes):
            q = _as_horizon_by_level(quantiles[i], h, len(lv))
            out.append(Forecast(float(c[-1]), lv, q.T))
        return out


class ChronosBoltForecaster(_Chronos):
    name: ClassVar[str] = "chronos_bolt"
    description: ClassVar[str] = "Chronos-Bolt small (Amazon, Apache-2.0), closes only."
    release_date: ClassVar[date | None] = date(2024, 11, 25)
    default_model_id: ClassVar[str] = "amazon/chronos-bolt-small"
    default_max_context: ClassVar[int] = 2048


class Chronos2Forecaster(_Chronos):
    name: ClassVar[str] = "chronos_2"
    description: ClassVar[str] = "Chronos-2 (Amazon, Apache-2.0), closes only."
    release_date: ClassVar[date | None] = date(2025, 10, 30)
    default_model_id: ClassVar[str] = "amazon/chronos-2"
    default_max_context: ClassVar[int] = 2048

"""TimesFM 2.5 adapter (Google, Apache-2.0 code and weights).

Install with ``uv sync --extra timesfm`` (the ``timesfm`` package with
torch). Only the 2.5 weights (``google/timesfm-2.5-200m-pytorch``,
released 2025-09-02) are allowed: the TimesFM 3.0 weights are under a
non-commercial, non-production licence, so the adapter refuses any model
id that names version 3. No pretraining end date is published, so the
cutoff is the release date.

TimesFM returns the mean and the deciles 0.1 to 0.9. Other levels are
interpolated between the deciles, and levels outside them take the
nearest decile.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import Any, ClassVar

import numpy as np
import pandas as pd

from stonks.features.forecasters._pretrained import PretrainedForecaster, interpolate_levels
from stonks.features.forecasters.base import DEFAULT_LEVELS, Forecast, _check_levels

__all__ = ["TimesFm25Forecaster"]

#: The deciles in columns 1 to 9 of TimesFM's quantile output (0 is the mean).
_DECILES = (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9)
#: Longest horizon compiled into the decoder.
_MAX_HORIZON = 256


class TimesFm25Forecaster(PretrainedForecaster):
    name: ClassVar[str] = "timesfm_2_5"
    description: ClassVar[str] = "TimesFM 2.5 200M (Google, Apache-2.0), closes only."
    licence: ClassVar[str] = "Apache-2.0"
    release_date: ClassVar[date | None] = date(2025, 9, 2)
    requires: ClassVar[tuple[str, ...]] = ("timesfm", "torch")
    extra: ClassVar[str | None] = "timesfm"
    default_model_id: ClassVar[str] = "google/timesfm-2.5-200m-pytorch"
    default_max_context: ClassVar[int] = 1024

    def __init__(self, model_id: str | None = None, **options: Any) -> None:
        super().__init__(model_id, **options)
        if "timesfm-3" in self.model_id.lower() or "timesfm3" in self.model_id.lower():
            raise ValueError(
                "TimesFM 3.0 weights are licensed for non-commercial, non-production use "
                "only: use the 2.5 weights (google/timesfm-2.5-200m-pytorch)"
            )

    def _load(self) -> Any:
        import timesfm

        model = timesfm.TimesFM_2p5_200M_torch.from_pretrained(
            self.model_id, local_files_only=self.local_files_only
        )
        model.compile(
            timesfm.ForecastConfig(
                max_context=self.max_context,
                max_horizon=_MAX_HORIZON,
                normalize_inputs=True,
                use_continuous_quantile_head=True,
                fix_quantile_crossing=True,
            )
        )
        return model

    def predict_batch(
        self,
        contexts: Sequence[pd.DataFrame],
        horizon: int,
        levels: Sequence[float] = DEFAULT_LEVELS,
    ) -> list[Forecast]:
        h = self._check_horizon(horizon)
        if h > _MAX_HORIZON:
            raise ValueError(f"horizon must be <= {_MAX_HORIZON} for TimesFM, got {h}")
        lv = _check_levels(levels)
        closes = self._closes(contexts)
        _point, quantiles = self.model().forecast(horizon=h, inputs=closes)
        q = np.asarray(quantiles, dtype=float)
        out = []
        for i, c in enumerate(closes):
            deciles = q[i, :h, 1 : 1 + len(_DECILES)].T  # levels x horizon
            out.append(Forecast(float(c[-1]), lv, interpolate_levels(_DECILES, deciles, lv)))
        return out

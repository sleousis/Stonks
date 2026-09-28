"""``[production.feature_drift]``: the live feature drift check of model
strategies (roadmap 23.10), read by the ``feature_drift`` tick hook."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class FeatureDriftSettings(BaseModel):
    """``[production.feature_drift]``: the live feature drift check."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    #: Trailing tick days of live rows compared with the training profile.
    window_days: int = Field(default=60, ge=1)
    #: Fewest live rows in the window before PSI is scored. PSI of a small
    #: sample is biased upwards (about ``(bins - 1) / rows`` with no drift
    #: at all), so keep this well above the profile's bin count.
    min_rows: int = Field(default=50, ge=2)
    #: PSI above this for any feature is a warning (0.25 is a real shift).
    warn_psi: float = Field(default=0.25, gt=0.0)

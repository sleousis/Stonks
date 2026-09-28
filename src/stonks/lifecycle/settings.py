"""``[lifecycle]`` settings (roadmap 22.6).

Example::

    [lifecycle]
    lookback_days = 730           # each fit trains on this many calendar days
    statuses = ["active", "shadow"]
    min_days_between_fits = 5     # a newer fit than this is kept, not redone

    [lifecycle.swap]
    min_days = 20                 # candidate model book days before a swap
    max_drawdown = 0.25
    max_underperformance = 0.02   # candidate may trail the live version by this much
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class SwapPolicy(BaseModel):
    """What a candidate version must show in its model book before a swap."""

    model_config = ConfigDict(extra="forbid")

    #: Days with a candidate model book snapshot.
    min_days: int = Field(default=20, ge=1)
    #: Deepest fall of the candidate book, as a positive fraction.
    max_drawdown: float = Field(default=0.25, gt=0.0, le=1.0)
    #: How far the candidate's return may trail the live version's over
    #: the same days (0.02 is two percentage points).
    max_underperformance: float = Field(default=0.02, ge=0.0)


class ModelLifecycleSettings(BaseModel):
    """``[lifecycle]``: scheduled retraining and the swap gate."""

    model_config = ConfigDict(extra="forbid")

    #: Calendar days of history each fit trains on, ending on the fit date.
    lookback_days: int = Field(default=730, ge=30)
    #: Registry statuses whose retrainable strategies the job refits.
    statuses: list[Literal["active", "shadow"]] = Field(
        default_factory=lambda: ["active", "shadow"]
    )
    #: Skip a strategy whose newest fit ended less than this many days ago
    #: (``force`` ignores it). Keeps a re-run on the same day a no-op.
    min_days_between_fits: int = Field(default=5, ge=0)
    swap: SwapPolicy = Field(default_factory=SwapPolicy)

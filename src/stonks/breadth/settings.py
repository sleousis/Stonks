"""``[breadth]``: how the market breadth card is computed (roadmap 23.14)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class BreadthSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    #: A stored universe to measure. ``None``: every equity with bars in the
    #: lake, funds and the index left out.
    universe_id: str | None = Field(default=None, max_length=64)
    #: The index for distribution days. ``None`` hides that line.
    index: str | None = Field(default="SPY.US", max_length=32)
    #: Sessions a stock needs before it counts for new highs and lows.
    min_history: int = Field(default=60, ge=2, le=252)
    #: Sessions in the high and low window (252 is about a year).
    high_low_window: int = Field(default=252, ge=20, le=520)
    #: Sessions counted for distribution days (IBD uses 25).
    distribution_window: int = Field(default=25, ge=1, le=100)
    #: An index fall at least this big (0.002 = 0.2 %) on higher volume.
    distribution_drop: float = Field(default=0.002, gt=0.0, lt=0.2)
    #: Calendar days of bars read (enough for the 200 day average).
    lookback_days: int = Field(default=400, ge=60, le=800)

"""``[lifecycle]`` settings (roadmap 22.6).

Example::

    [lifecycle]
    lookback_days = 730           # each fit trains on this many calendar days
    statuses = ["active", "shadow"]
    min_days_between_fits = 5     # a newer fit than this is kept, not redone

    [lifecycle.swap]
    min_days = 20                 # candidate model book days before a swap
    max_drawdown = 0.25
    vs_live_alpha = 0.05          # refuse when the candidate trails live with p below this
    min_paired_days = 10          # paired daily returns the vs_live test needs
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from stonks.logging import get_logger

_log = get_logger("stonks.lifecycle.settings")


class SwapPolicy(BaseModel):
    """What a candidate version must show in its model book before a swap."""

    model_config = ConfigDict(extra="forbid")

    #: Days with a candidate model book snapshot.
    min_days: int = Field(default=20, ge=1)
    #: Deepest fall of the candidate book, as a positive fraction.
    max_drawdown: float = Field(default=0.25, gt=0.0, le=1.0)
    #: The ``vs_live`` check is a paired test on the two books' daily
    #: returns over the same days (a HAC t-test of the mean daily gap,
    #: roadmap 23.9). It fails when the candidate trails the live version
    #: with a one-sided p-value below this.
    vs_live_alpha: float = Field(default=0.05, gt=0.0, lt=0.5)
    #: Paired daily returns the test needs before it can pass.
    min_paired_days: int = Field(default=10, ge=2)

    @model_validator(mode="before")
    @classmethod
    def _drop_raw_gap(cls, data: Any) -> Any:
        """``max_underperformance`` (a raw return gap) gave way to the
        paired test in 23.9. An old config still loads, with a warning."""
        if isinstance(data, dict) and "max_underperformance" in data:
            data = {k: v for k, v in data.items() if k != "max_underperformance"}
            _log.warning("lifecycle.swap.max_underperformance_ignored")
        return data


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

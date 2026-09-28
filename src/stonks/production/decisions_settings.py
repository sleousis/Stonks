"""``[production.decisions]``: the store of why a ticker did or did not
trade (roadmap 23.7). A light module (pydantic only) for ``stonks.config``."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class DecisionSettings(BaseModel):
    """Why did or didn't we trade: one row per tick, book and ticker."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = True
    #: Days of rows kept. Older rows go after each real tick.
    keep_days: int = Field(default=120, ge=1, le=3650)

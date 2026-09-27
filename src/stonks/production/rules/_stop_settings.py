"""Settings of the broker-side protective stops (roadmap 19.10).

They sit next to the risk rules, under
``[production.risk.rules.protective_stops]``, so a portfolio override and
a subscription (one strategy's slice) can only tighten them
(``production.rules.settings.MERGE_RULES``): switching stops on is tighter,
and so is a smaller ATR multiple (a closer stop). The stops themselves are
placed by ``production.live.stops``, not by a risk rule.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ProtectiveStopSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    #: Off by default. On: every position Stonks opens gets a good till
    #: cancelled stop at the broker once its entry fills.
    enabled: bool = False
    #: The stop sits this many ATRs from the entry price (below a long,
    #: above a short).
    atr_multiple: float = Field(default=3.0, gt=0.0, le=50.0)
    #: Daily bars in the Wilder ATR.
    atr_window: int = Field(default=14, ge=2, le=250)
    #: The distance as a share of the entry price when the ATR cannot be
    #: computed (too few bars).
    fallback_pct: float = Field(default=0.10, gt=0.0, lt=1.0)

    @property
    def active(self) -> bool:
        return self.enabled

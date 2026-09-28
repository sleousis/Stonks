"""``[production.price_check]``: the second-source price check (roadmap 23.6)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class PriceCheckSettings(BaseModel):
    """Before the tick, the vendor's latest closes and adjusted returns of
    held and signalled tickers are compared with a second source. Off by
    default: it asks the second source over the network."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    #: The second source, any ``--source`` id (``yahoo`` needs no key).
    source: str = "yahoo"
    #: Held tickers of a portfolio on an IB Gateway are checked against
    #: IBKR's marks instead (closes only).
    use_broker_marks: bool = True
    #: A close that differs by more than this share is a gap.
    max_close_gap: float = Field(default=0.02, gt=0, le=1)
    #: Adjusted returns over ``adjustment_window_days`` that differ by more
    #: than this share are a gap (a split or dividend applied by one source
    #: and not the other).
    max_adjustment_gap: float = Field(default=0.05, gt=0, le=1)
    adjustment_window_days: int = Field(default=30, ge=5, le=400)
    #: When at least this share of the compared tickers gap, and at least
    #: ``min_systematic_tickers`` were compared, the gap is systematic: the
    #: global operational halt opens.
    systematic_share: float = Field(default=0.5, gt=0, le=1)
    min_systematic_tickers: int = Field(default=3, ge=1)
    #: At most this many tickers per run (held first, then signalled).
    max_tickers: int = Field(default=200, ge=1)

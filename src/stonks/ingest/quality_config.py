"""Settings for bar validation and source fallback on ingest (roadmap 12.5).

Two ``[ingest.*]`` sub-models, kept out of ``stonks.config`` so the ingest
block owns them; ``Settings`` mounts them as ``ingest.quality`` and
``ingest.fallback``.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class DataQualityConfig(BaseModel):
    """Bar validation thresholds (``[ingest.quality]``).

    Rows that break a hard rule (missing or non-positive prices, high below
    low, close outside [low, high], duplicate timestamps, one-bar spikes
    that revert) go to ``quarantined_bars`` instead of the bar store.
    Softer findings (a large move that does not revert, calendar gaps, a
    stale or flat series, zero-volume streaks) are warnings only: they can
    be real, so they never drop data.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    # A bar is a spike when its log return exceeds both ``spike_sigmas``
    # robust sigmas (1.4826 x MAD of the series' log returns) and
    # ``spike_min_move``, and the next bar takes most of it back.
    spike_sigmas: float = Field(default=10.0, gt=0)
    spike_min_move: float = Field(default=0.25, gt=0)
    # Floor for the robust sigma, so a nearly flat series does not turn
    # every tick into a spike.
    min_sigma: float = Field(default=0.005, gt=0)
    # Returns needed (history + batch) before spikes are judged at all.
    min_history_bars: int = Field(default=20, ge=2)
    # How many stored bars before the batch are read as context.
    history_bars: int = Field(default=60, ge=0)
    # Relative slack for close vs [low, high] (vendor rounding).
    range_tolerance: float = Field(default=0.0005, ge=0)
    # Daily series whose newest bar is older than this (calendar days
    # before the requested end, or today) are flagged stale.
    stale_after_days: int = Field(default=7, ge=1)
    flat_price_streak: int = Field(default=10, ge=2)
    zero_volume_streak: int = Field(default=5, ge=2)
    # Alert (via the Notifier) when a run quarantines at least this many
    # rows (0 disables), when at least this many tickers carry warnings
    # (0 disables), and whenever a fallback source supplied data.
    alert_quarantined_rows: int = Field(default=1, ge=0)
    alert_warned_tickers: int = Field(default=5, ge=0)
    alert_on_fallback: bool = True


class FallbackConfig(BaseModel):
    """Secondary source per primary (``[ingest.fallback]``), e.g.
    ``sources = { eodhd = "yahoo" }``: a ticker whose bar fetch soft-fails
    on the primary is retried once on the fallback. Empty (the default)
    disables fallback."""

    model_config = ConfigDict(extra="forbid")

    sources: dict[str, str] = Field(default_factory=dict)

    def for_primary(self, primary_id: str) -> str | None:
        fallback = self.sources.get(primary_id)
        if fallback is None or fallback == primary_id:
            return None
        return fallback

"""``[screener]`` settings (roadmap 20.11): how big a screen may be, when the
console runs it as a background job, and how long a result is reused."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["ScreenerSettings"]


class ScreenerSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: A screen whose rule keeps more candidates than this fails with a
    #: message that says how to narrow it.
    max_candidates: int = Field(default=10_000, ge=1, le=1_000_000)
    #: Above this many candidates the console runs the screen as a
    #: background job with progress. Direct runs still work up to the cap.
    job_threshold: int = Field(default=1_000, ge=0, le=1_000_000)
    #: Seconds a result is reused for the same spec and date (0: off).
    cache_seconds: float = Field(default=300.0, ge=0, le=86_400)
    #: Results kept in the cache (the oldest goes first).
    cache_entries: int = Field(default=32, ge=1, le=1_000)

"""``[ensure]`` settings for :class:`~stonks.ingest.ensure.DataEnsurer`.

A module of its own (no pipeline imports) so ``stonks.config`` can load it
without an import cycle. ``stonks.ingest.ensure`` re-exports it.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class EnsureSettings(BaseModel):
    """How the ensurer fetches. Owned by the ingest block; the app reads it
    from the ``[ensure]`` settings section."""

    #: Concurrent fetches (network bound, so more than the core count helps).
    max_workers: int = Field(default=8, ge=1, le=64)
    #: Requests per second per source id; others use the default.
    requests_per_second: dict[str, float] = Field(
        default_factory=lambda: {"eodhd": 10.0, "yahoo": 2.0}
    )
    default_requests_per_second: float = Field(default=5.0, gt=0)
    #: Data plan per source id (see :func:`stonks.ingest.ensure.vendor_limits`).
    #: EODHD defaults to the free tier, the safe assumption.
    plans: dict[str, str] = Field(default_factory=lambda: {"eodhd": "free"})
    #: Use one ``fetch_bulk_eod`` call per exchange and day when the plan allows.
    bulk: bool = False
    #: Bulk only when an exchange misses at most this many business days...
    bulk_max_days: int = Field(default=5, ge=1, le=31)
    #: ...across at least this many tickers.
    bulk_min_tickers: int = Field(default=20, ge=1)
    #: Fetches kept in flight ahead of the writer (bounds memory).
    prefetch_window: int = Field(default=64, ge=1)
    #: Empty answers for the last this-many days are asked again next time
    #: (the vendor may not have published them yet).
    settle_days: int = Field(default=14, ge=0)

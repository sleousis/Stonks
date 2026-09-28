"""``[production.intraday_pnl]`` (roadmap 21.3.3), apart from
:mod:`stonks.production.intraday_pnl` so ``stonks.config`` can mount it
without importing the ledger (which imports the config)."""

from __future__ import annotations

from pydantic import BaseModel, Field

__all__ = ["IntradayPnlSettings"]


class IntradayPnlSettings(BaseModel):
    """``[production.intraday_pnl]``: marks and intraday snapshots."""

    enabled: bool = False
    #: Store one row per book this often (bar closes in between update the
    #: high-water mark only).
    snapshot_minutes: int = Field(default=5, ge=1, le=390)
    #: A held name whose mark is older than this counts as stale.
    stale_mark_seconds: int = Field(default=120, ge=1)

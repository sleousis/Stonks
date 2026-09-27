"""``[engine]`` settings: the intraday engine process (roadmap 21.2.5).

Example::

    [engine]
    enabled = true
    universe = ["AAPL.US", "MSFT.US"]

    [[engine.books]]
    id = "orb"
    portfolio_id = "pf_intraday"
    strategies = ["opening_range_breakout"]
    broker = "simulated"
    initial_cash = 100000
    sessions = { flatten_at_close = true, flatten_minutes = 5 }

Everything is off by default: the scheduler jobs ``engine_start`` and
``engine_stop`` skip while ``enabled = false``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from stonks.engine.sessions import SessionRules
from stonks.portfolio.settings import ConstructionSettings

EngineBrokerKind = Literal["simulated", "ibkr"]


class EngineBookSettings(BaseModel):
    """One intraday book: a portfolio, its strategies and its broker."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9_.-]+$")
    #: The portfolio its orders and fills belong to in the ledger.
    portfolio_id: str = Field(min_length=1)
    #: Registry strategy ids (their stored params) or catalog names (defaults).
    strategies: list[str] = Field(min_length=1)
    broker: EngineBrokerKind = "simulated"
    #: Starting cash of a simulated book. A restart rebuilds the book from
    #: this plus the ledger's fills.
    initial_cash: float = Field(default=100_000.0, gt=0)
    construction: ConstructionSettings = Field(default_factory=ConstructionSettings)
    sessions: SessionRules = Field(default_factory=SessionRules)


class EngineSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    #: The decision bar (an intraday ``Interval`` code).
    interval: str = "1m"
    #: The calendar the scheduler jobs and the auto stop follow.
    calendar: str = "XNYS"
    #: The live stream source (``None``: ``[streaming].source``).
    source: str | None = None
    #: Tickers the engine decides on (``[]``: ``[streaming].tickers``).
    universe: list[str] = Field(default_factory=list)
    books: list[EngineBookSettings] = Field(default_factory=list)
    #: ``engine_start`` fires this many minutes before the open.
    start_before_open_minutes: int = Field(default=15, ge=0)
    #: ``engine_stop`` fires this many minutes after the close, and the
    #: process stops itself then too.
    stop_after_close_minutes: int = Field(default=10, ge=0)
    #: No new entries on a ticker whose last bar is older than this.
    stale_after_seconds: float | None = Field(default=180.0, gt=0)
    #: Score threshold of the decision step.
    threshold: float = 0.0
    #: Control files (lock, stop request, log). Default: ``engine`` next
    #: to the state DB.
    control_dir: Path | None = None
    #: How long ``engine_stop`` waits for the process to exit.
    stop_timeout_seconds: float = Field(default=60.0, gt=0)

    @model_validator(mode="after")
    def _unique_books(self) -> EngineSettings:
        ids = [b.id for b in self.books]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"duplicate engine book ids: {dupes}")
        pids = [b.portfolio_id for b in self.books]
        shared = sorted({p for p in pids if pids.count(p) > 1})
        if shared:
            raise ValueError(f"engine books must not share a portfolio: {shared}")
        return self


def control_dir_for(engine: EngineSettings, state_path: str | Path) -> Path:
    """Where the engine keeps its control files."""
    if engine.control_dir is not None:
        return Path(engine.control_dir)
    return Path(state_path).parent / "engine"

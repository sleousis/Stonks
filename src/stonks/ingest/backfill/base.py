"""The :class:`BackfillPhase` seam and what a phase gets to work with.

A phase is one module in :mod:`stonks.ingest.backfill.phases` with one
concrete subclass. It lists its units, says roughly what a unit costs,
fetches a unit (on a fetch thread: network only, no lake or state) and
writes fetched units (on the runner's thread, through the ingest pipeline).
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, ClassVar

from stonks.ingest.backfill.settings import BackfillSettings
from stonks.ingest.backfill.state import BackfillState, Unit, UnitSpec
from stonks.ingest.pipeline import IngestPipeline, IngestRunResult
from stonks.ingest.sources.base import DataSource, DataSourceError
from stonks.store.lake import DuckDBLake


@dataclass(frozen=True)
class Outcome:
    """What writing one unit came to. ``cursor`` set and ``finished``
    false: a step of a unit that runs in steps, to resume at ``cursor``.
    ``error``: the unit could not be stored (a retry follows)."""

    rows: int = 0
    finished: bool = True
    cursor: str | None = None
    error: str | None = None
    note: str | None = None


@dataclass
class BackfillContext:
    source: DataSource
    lake: DuckDBLake
    state: BackfillState
    settings: BackfillSettings
    #: An ingest pipeline over ``source`` (the real one or a replay of
    #: fetched rows) writing to the lake.
    make_pipeline: Callable[[DataSource], IngestPipeline]
    #: Only units of these exchanges (``None``: every exchange).
    exchanges: frozenset[str] | None = None
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    #: Builds the free sources the unmetered phases read (``edgar``,
    #: ``ibkr_borrow``, ``defillama``). It raises ``DataSourceError`` when a
    #: source is not set up (EDGAR without a user agent).
    sources: Callable[[str], DataSource] | None = None
    _built: dict[str, DataSource] = field(default_factory=dict, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def source_for(self, source_id: str) -> DataSource:
        """The free source ``source_id``, built once."""
        with self._lock:
            if source_id not in self._built:
                if self.sources is None:
                    raise DataSourceError(f"no {source_id} source is set up for the backfill")
                self._built[source_id] = self.sources(source_id)
            return self._built[source_id]

    @property
    def today(self) -> date:
        return self.clock().astimezone(UTC).date()


class BackfillPhase(ABC):
    #: Stable id, stored with each unit (``--phases``).
    phase_id: ClassVar[str]
    #: Phases run in this order, lowest first.
    order: ClassVar[int]
    title: ClassVar[str]
    #: Units fetched and written together (bounds memory).
    batch_size: ClassVar[int] = 10
    #: How long a listing stays good; ``None``: list again on every run
    #: (cheap phases that read the lake).
    relist_after: ClassVar[timedelta | None] = None
    #: ``False``: the phase reads a free source with its own rate limit, not
    #: the metered vendor. It spends no budget, so it also runs while the
    #: day's budget is used up (until the reset).
    metered: ClassVar[bool] = True

    @abstractmethod
    def list_units(self, ctx: BackfillContext) -> Iterable[UnitSpec]:
        """Every unit of the phase. Stored units keep their progress."""

    @abstractmethod
    def estimate(self, unit: Unit) -> int:
        """Roughly what fetching ``unit`` costs in API calls."""

    @abstractmethod
    def fetch(self, ctx: BackfillContext, unit: Unit) -> Any:
        """Fetch ``unit`` (fetch thread: no lake, no state)."""

    @abstractmethod
    def write(
        self, ctx: BackfillContext, fetched: Sequence[tuple[Unit, Any]]
    ) -> dict[str, Outcome]:
        """Store fetched units (runner thread); the outcome per unit key."""

    def eta_cost(self, settings: BackfillSettings, today: date) -> float:
        """Calls one whole unit should take, for the ETA before any unit is
        done (default: one step, :meth:`estimate`)."""
        del settings, today
        probe = Unit(
            phase=self.phase_id, key="", exchange=None, priority=0, cursor=None, attempts=0
        )
        return float(self.estimate(probe))

    def listing_ttl(self, ctx: BackfillContext) -> timedelta | None:
        """How long a listing stays good (default :attr:`relist_after`)."""
        del ctx
        return self.relist_after

    def ready(self, ctx: BackfillContext) -> str | None:
        """Why the phase cannot run now (``None``: it can)."""
        del ctx
        return None


# ---- helpers phases share ---------------------------------------------------------


def exchange_of(ticker: str) -> str | None:
    """The exchange code of a ``CODE.EXCHANGE`` ticker."""
    base, dot, suffix = ticker.rpartition(".")
    return suffix.upper() if dot and base else None


def pipeline_outcomes(
    result: IngestRunResult, keys: Collection[str], rows: dict[str, int]
) -> dict[str, Outcome]:
    """Outcomes of a pipeline run over ``keys``: the units the pipeline
    could not store get an error naming the run."""
    failed = set(result.failed)
    error = f"the ingest pipeline could not store it (ingest run {result.run_id})"
    return {k: Outcome(error=error) if k in failed else Outcome(rows=rows.get(k, 0)) for k in keys}


def instrument_rows(
    ctx: BackfillContext,
    *,
    security_types: Collection[str] | None = None,
    asset_classes: Collection[str] | None = None,
    exchanges: Collection[str] | None = None,
    include_delisted: bool = True,
) -> list[tuple[str, str | None, str | None, bool]]:
    """``(ticker, asset_class, security_type, is_delisted)`` of the lake's
    instruments that match, skipping the exchanges the settings skip."""
    where = ["1 = 1"]
    params: list[Any] = []
    if security_types is not None:
        where.append("security_type = ANY(?)")
        params.append(list(security_types))
    if asset_classes is not None:
        where.append("asset_class = ANY(?)")
        params.append(list(asset_classes))
    if not include_delisted:
        where.append("COALESCE(is_delisted, FALSE) = FALSE")
    frame = ctx.lake.con.execute(
        "SELECT id, asset_class, security_type, COALESCE(is_delisted, FALSE) AS delisted "
        f"FROM instruments WHERE {' AND '.join(where)} ORDER BY id",
        params,
    ).fetchall()
    skip = {e.upper() for e in ctx.settings.skip_exchanges}
    wanted = {e.upper() for e in exchanges} if exchanges is not None else None
    out: list[tuple[str, str | None, str | None, bool]] = []
    for ticker, asset_class, security_type, delisted in frame:
        exchange = exchange_of(ticker)
        if exchange in skip:
            continue
        if wanted is not None and exchange not in wanted:
            continue
        out.append((ticker, asset_class, security_type, bool(delisted)))
    return out


#: Priority by equity kind: stocks first, then ETFs, then the rest, funds last.
_KIND_PRIORITY = {
    "common_stock": 0,
    "adr": 1,
    "preferred_stock": 2,
    "etf": 3,
    "other": 5,
    "fund": 8,
}


def instrument_priority(security_type: str | None, delisted: bool) -> int:
    """Lower runs first: active before delisted, stocks before funds."""
    return _KIND_PRIORITY.get(security_type or "", 4) * 2 + (1 if delisted else 0)

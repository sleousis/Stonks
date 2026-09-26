"""The universe seam: definitions, membership spans and the provider ABC.

A :class:`UniverseDefinition` is a stored, named universe. Its ``kind``
picks a :class:`UniverseProvider`, which validates the ``spec`` and turns
it into :class:`MembershipSpan` rows (:meth:`UniverseProvider.materialize`).
A refresh (:func:`stonks.universes.refresh.refresh_universe`) writes those
spans to ``universe_membership``, where the lab and the tick read them
point in time (principle P14).

Adding a kind is one new module in :mod:`stonks.universes.providers` with
a :class:`UniverseProvider` subclass; the registry
(:mod:`stonks.universes.registry`) discovers it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:  # pragma: no cover
    from stonks.ingest.schemas import TickerProfile
    from stonks.ingest.sources.base import DataSource
    from stonks.store.lake import DuckDBLake
    from stonks.universes.index_sources.base import IndexSource

UniverseKind = Literal["list", "exchange", "rule", "index"]

#: The first day of an open-ended static membership.
EARLIEST = date(1900, 1, 1)

#: Universe ids: lower case, digits, ``_ . -``; they appear in URLs.
UNIVERSE_ID_PATTERN = r"^[a-z0-9][a-z0-9_.-]{0,63}$"


@dataclass(frozen=True)
class MembershipSpan:
    """A ticker is a member on day ``d`` when ``start_date <= d`` and
    (``end_date`` is ``None`` or ``d < end_date``), as in migration 015."""

    ticker: str
    start_date: date
    end_date: date | None = None

    def __post_init__(self) -> None:
        if self.end_date is not None and self.end_date <= self.start_date:
            raise ValueError(
                f"{self.ticker}: end_date {self.end_date} must be after start_date "
                f"{self.start_date}"
            )


@dataclass(frozen=True)
class IndexChange:
    """One addition to (``add``) or removal from (``remove``) an index,
    effective on ``change_date``."""

    ticker: str
    change_date: date
    action: Literal["add", "remove"]


@dataclass(frozen=True)
class IndexHistory:
    """An index's members on ``as_of`` plus its change history. Either part
    may be empty (``as_of`` is ``None`` without a snapshot)."""

    index_id: str
    as_of: date | None
    constituents: tuple[str, ...] = ()
    changes: tuple[IndexChange, ...] = ()
    source: str | None = None


class UniverseDefinition(BaseModel):
    """A stored universe. ``spec`` is kind-specific; :meth:`validated`
    returns it parsed by the kind's provider (``ValueError`` when it is
    invalid)."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=UNIVERSE_ID_PATTERN)
    kind: UniverseKind
    name: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    spec: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None
    updated_at: datetime | None = None
    refreshed_at: datetime | None = None
    member_count: int | None = None

    def validated(self) -> BaseModel:
        from stonks.universes.registry import provider_for

        return provider_for(self.kind).parse_spec(self.spec)


SourceFactory = Callable[[str | None], "DataSource"]
IndexSourceFactory = Callable[[str], "IndexSource"]


@dataclass
class RefreshContext:
    """What a provider may read while it materialises a definition.

    ``source`` builds a :class:`DataSource` by id (``None``: the default
    source); ``index_source`` builds an index adapter by id. Providers that
    need one and get ``None`` raise ``ValueError``. ``as_of`` is the refresh
    date (open spans and undated delistings are judged against it)."""

    lake: DuckDBLake
    as_of: date
    source: SourceFactory | None = None
    index_source: IndexSourceFactory | None = None


@dataclass
class Materialized:
    """A provider's output. ``profiles`` (instrument rows learned from a
    source) and ``index_history`` (a fetched history to store) are written
    by the refresh in the same transaction as the spans."""

    spans: list[MembershipSpan]
    warnings: list[str] = field(default_factory=list)
    profiles: list[TickerProfile] = field(default_factory=list)
    index_history: IndexHistory | None = None


class UniverseProvider(ABC):
    """Turns one kind of universe spec into membership spans."""

    #: The ``universe_definitions.kind`` this provider serves.
    kind: ClassVar[str]
    #: Pydantic model of the spec (``extra="forbid"`` so typos fail).
    spec_model: ClassVar[type[BaseModel]]

    def parse_spec(self, spec: Mapping[str, Any]) -> BaseModel:
        """The validated spec; ``ValueError`` (pydantic's) when invalid."""
        return self.spec_model.model_validate(dict(spec))

    @abstractmethod
    def materialize(self, spec: Any, ctx: RefreshContext) -> Materialized:
        """Membership spans for ``spec`` (a ``spec_model`` instance). Reads
        only: the refresh does every write."""

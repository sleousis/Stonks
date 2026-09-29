"""The ``StatementPreset`` seam: a ready reader for one broker export.

The generic CSV import (:mod:`stonks.connections.statement_csv`) asks the
person to map columns. A preset knows one broker's export already: its
headers in every language the broker writes, its number and date formats,
and how its rows turn into activities (or, for a holdings export, into
holdings). A preset is one module in this package; adding one never edits a
list (see :mod:`stonks.connections.statement_presets.registry`).

Presets read only files a person exported by hand. They never talk to a
broker.

Instruments come in by identifier (an ISIN plus the broker's exchange and
currency). An :class:`InstrumentResolver` turns them into our tickers
through the ``instruments`` identifiers in the lake. What it cannot map
stays in the import by its identifier and is listed as not covered.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import ClassVar, Literal, Protocol

from stonks.connections.statement_csv import ParsedRow

PresetKind = Literal["activities", "holdings"]


@dataclass(frozen=True)
class Listing:
    """One instrument as an export names it: an ISIN, the listing's
    exchange as our ticker suffix (``US``, ``AS``, ``XETRA``; ``None`` when
    unknown) and the trading currency."""

    isin: str
    suffix: str | None = None
    currency: str | None = None


class InstrumentResolver(Protocol):
    """Our ticker for each listing, or ``None`` when the lake has no
    instrument with that ISIN or cannot tell its listings apart."""

    def resolve(self, listings: Sequence[Listing]) -> Mapping[Listing, str | None]: ...


class NoResolver:
    """Maps nothing: every instrument stays by its identifier."""

    def resolve(self, listings: Sequence[Listing]) -> Mapping[Listing, str | None]:
        return dict.fromkeys(listings)


@dataclass(frozen=True)
class ParsedHolding:
    """One line of a holdings export: a position, or a cash balance."""

    line: int
    row_id: str
    raw_symbol: str
    ticker: str | None
    quantity: float
    price: float | None
    market_value: float | None
    currency: str | None
    description: str | None
    is_cash: bool = False


@dataclass
class PresetParse:
    """What a preset read from one file."""

    preset: str
    kind: PresetKind
    #: The file's language, as a short code (``en``, ``nl``, ``de``, ...).
    locale: str
    rows: list[ParsedRow] = field(default_factory=list)
    holdings: list[ParsedHolding] = field(default_factory=list)
    #: Lines a holdings export could not read, with the reason.
    skipped: list[ParsedRow] = field(default_factory=list)
    #: Plain notes for the person (what was left out on purpose, and why).
    notes: list[str] = field(default_factory=list)
    #: Not covered identifiers with the export's product name.
    unmapped: dict[str, str] = field(default_factory=dict)

    @property
    def total(self) -> int:
        if self.kind == "holdings":
            return len(self.holdings) + len(self.skipped)
        return len(self.rows)


class StatementPreset(ABC):
    """One broker export. Subclasses set the class attributes and read the
    text; they register with
    :func:`~stonks.connections.statement_presets.registry.register_preset`."""

    #: Stable id, such as ``degiro_transactions``.
    id: ClassVar[str]
    #: The broker's name as people know it.
    broker: ClassVar[str]
    #: The export's name, such as ``Transactions``.
    label: ClassVar[str]
    kind: ClassVar[PresetKind]
    #: Where to find the export in the broker's site or app, in one or two sentences.
    how_to_export: ClassVar[str]

    @abstractmethod
    def detect(self, headers: Sequence[str]) -> str | None:
        """The file's locale when ``headers`` are this export's, else ``None``."""

    @abstractmethod
    def parse(self, text: str, resolver: InstrumentResolver) -> PresetParse:
        """Read the whole file. Raises
        :class:`~stonks.connections.statement_csv.StatementError` when it
        cannot be read at all."""

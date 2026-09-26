"""The index-source seam: fetch an index's constituents and change history.

An :class:`IndexSource` returns a vendor-agnostic
:class:`~stonks.universes.base.IndexHistory` with canonical tickers
(``AAPL.US``); the vendor's page or API shape stays inside the adapter.
Adapters are discovered from this package by ``source_id``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar

from stonks.universes.base import IndexHistory


class IndexSourceError(RuntimeError):
    """The adapter could not fetch or parse the history."""


class IndexSource(ABC):
    source_id: ClassVar[str]
    #: Index codes this source serves (e.g. ``("sp500",)``).
    index_ids: ClassVar[tuple[str, ...]]

    @abstractmethod
    def fetch(self, index_id: str) -> IndexHistory:
        """The current constituents plus the change history of ``index_id``."""

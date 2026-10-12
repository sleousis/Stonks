"""Index members and their history from the configured EODHD source.

EODHD serves, for each index it lists under ``INDX``, the members today
and every past member with its start and end dates (delisted names too),
so a stored index universe rebuilds membership on any past day without
survivorship bias. The vendor's payload is parsed in the ingest adapter
(``stonks.ingest.sources.eodhd_reference``); this module only turns the
vendor-neutral rows into an :class:`IndexHistory`.

Index ids are canonical (``sp500`` for ``GSPC``); any other id is the
vendor code in lower case (``ftse`` for ``FTSE``). The backfill's
``index_constituents`` phase stores the same histories, so a universe with
``source = "stored"`` reads them without a call.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from typing import ClassVar

import requests

from stonks.ingest.sources.base import DataSource, DataSourceError
from stonks.universes.base import IndexChange, IndexHistory
from stonks.universes.index_sources.base import IndexSource, IndexSourceError


def _default_source() -> DataSource:
    from stonks.config import load_settings
    from stonks.ingest.sources.registry import build_eodhd

    return build_eodhd(load_settings().sources)


class EodhdIndexSource(IndexSource):
    source_id = "eodhd"
    #: Every index EODHD lists; ids are checked by the vendor.
    index_ids: ClassVar[tuple[str, ...]] = ()

    def __init__(
        self,
        source: DataSource | None = None,
        today: Callable[[], date] = date.today,
    ) -> None:
        self._source = source
        self._today = today

    def fetch(self, index_id: str) -> IndexHistory:
        from stonks.ingest.sources.eodhd_reference import vendor_index_code

        try:
            source = self._source or _default_source()
            row = source.fetch_index_components(vendor_index_code(index_id), self._today())
        except (DataSourceError, requests.RequestException, ValueError) as exc:
            raise IndexSourceError(f"could not fetch index {index_id!r} from EODHD: {exc}") from exc
        if not row.members and not row.changes:
            raise IndexSourceError(f"EODHD lists no members for index {index_id!r}")
        return IndexHistory(
            index_id=index_id,
            as_of=row.as_of if row.members else None,
            constituents=row.members,
            changes=tuple(IndexChange(c.ticker, c.change_date, c.action) for c in row.changes),
            source=self.source_id,
        )

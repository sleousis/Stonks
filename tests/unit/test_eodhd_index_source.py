"""The EODHD index source turns vendor-neutral index rows into a history a
stored index universe rebuilds membership from, delisted names included.
Hermetic: a fake data source answers."""

from __future__ import annotations

from datetime import date

import pytest

from stonks.ingest.reference_schemas import IndexChangeRow, IndexComponentsRow
from stonks.ingest.sources.base import DataSource, DataSourceError
from stonks.universes.index_sources.base import IndexSourceError
from stonks.universes.index_sources.eodhd_index import EodhdIndexSource
from stonks.universes.providers.index import spans_from_index_history
from stonks.universes.registry import build_index_source, index_source_ids

TODAY = date(2026, 10, 12)


class _Source(DataSource):
    source_id = "eodhd"

    def __init__(self, row: IndexComponentsRow | Exception) -> None:
        self.row = row
        self.asked: list[str] = []

    def list_tickers(self, exchange):  # pragma: no cover - unused
        return []

    def fetch_prices(self, ticker, since=None, until=None):  # pragma: no cover
        return []

    def fetch_fundamentals(self, ticker):  # pragma: no cover
        raise NotImplementedError

    def fetch_index_components(self, index_code, as_of):
        self.asked.append(index_code)
        if isinstance(self.row, Exception):
            raise self.row
        return self.row


def _row() -> IndexComponentsRow:
    return IndexComponentsRow(
        index_id="sp500",
        as_of=TODAY,
        members=("AIZ.US",),
        changes=(
            IndexChangeRow(ticker="AIZ.US", change_date=date(2007, 4, 10), action="add"),
            IndexChangeRow(ticker="AAL.US", change_date=date(2015, 3, 23), action="add"),
            IndexChangeRow(ticker="AAL.US", change_date=date(2024, 9, 23), action="remove"),
        ),
    )


def test_it_is_discovered_by_the_registry():
    assert "eodhd" in index_source_ids()
    assert isinstance(build_index_source("eodhd", source=_Source(_row())), EodhdIndexSource)


def test_history_rebuilds_past_members_without_survivorship_bias():
    source = _Source(_row())
    history = EodhdIndexSource(source, today=lambda: TODAY).fetch("sp500")
    assert source.asked == ["GSPC"]  # the vendor code of the canonical id
    assert history.source == "eodhd" and history.as_of == TODAY
    spans, warnings = spans_from_index_history(history)
    assert warnings == []
    by_ticker = {s.ticker: (s.start_date, s.end_date) for s in spans}
    assert by_ticker["AAL.US"] == (date(2015, 3, 23), date(2024, 9, 23))  # left the index
    assert by_ticker["AIZ.US"] == (date(2007, 4, 10), None)


def test_vendor_failures_and_empty_indices_raise_an_index_source_error():
    with pytest.raises(IndexSourceError, match="could not fetch"):
        EodhdIndexSource(_Source(DataSourceError("down"))).fetch("ftse")
    empty = IndexComponentsRow(index_id="ftse", as_of=TODAY)
    with pytest.raises(IndexSourceError, match="no members"):
        EodhdIndexSource(_Source(empty)).fetch("ftse")

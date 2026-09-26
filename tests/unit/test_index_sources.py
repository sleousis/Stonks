"""Index history import (CSV/JSON), the list CSV parser and the Wikipedia
S&P 500 adapter over a saved page: no network."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from stonks.universes import IndexChange, build_index_source, index_source_ids
from stonks.universes.index_import import parse_index_history
from stonks.universes.index_sources.base import IndexSourceError
from stonks.universes.index_sources.wikipedia_sp500 import (
    WikipediaSp500Source,
    parse_sp500_page,
)
from stonks.universes.providers.index import spans_from_index_history
from stonks.universes.providers.static_list import parse_list_csv

FIXTURE = Path(__file__).parent.parent / "fixtures" / "wikipedia" / "sp500_sample.html"


# ---- CSV / JSON import ---------------------------------------------------------


def test_parse_index_history_csv():
    content = """date,ticker,action
2024-01-02,A.US,member
2024-01-02,B.US,member
2023-06-01,B.US,add
2023-06-01,C.US,remove
"""
    history = parse_index_history(content, "csv", index_id="toy")
    assert history.index_id == "toy"
    assert history.as_of == date(2024, 1, 2)
    assert history.constituents == ("A.US", "B.US")
    assert set(history.changes) == {
        IndexChange("B.US", date(2023, 6, 1), "add"),
        IndexChange("C.US", date(2023, 6, 1), "remove"),
    }


def test_parse_index_history_json():
    content = json.dumps(
        {
            "as_of": "2024-01-02",
            "constituents": ["A.US"],
            "changes": [{"date": "2023-06-01", "ticker": "C.US", "action": "remove"}],
        }
    )
    history = parse_index_history(content, "json", index_id="toy")
    assert history.constituents == ("A.US",)
    assert history.changes == (IndexChange("C.US", date(2023, 6, 1), "remove"),)


@pytest.mark.parametrize(
    "content",
    [
        "date,ticker,action\n2024-01-02,A.US,bogus\n",
        "date,ticker\n2024-01-02,A.US\n",
        "",
        "date,ticker,action\n2024-01-02,A.US,member\n2023-01-02,B.US,member\n",
    ],
)
def test_parse_index_history_rejects_bad_csv(content):
    with pytest.raises(ValueError):
        parse_index_history(content, "csv", index_id="toy")


# ---- list CSV ----------------------------------------------------------------------


def test_parse_list_csv_plain_and_dated():
    spec = parse_list_csv(
        "ticker,start_date,end_date\nA.US,,\nB.US,2010-01-01,2012-01-01\n# comment\nC.US,2015-01-01,\n"
    )
    assert spec["tickers"] == ["A.US"]
    assert spec["spans"] == [
        {"ticker": "B.US", "start_date": "2010-01-01", "end_date": "2012-01-01"},
        {"ticker": "C.US", "start_date": "2015-01-01"},
    ]


def test_parse_list_csv_without_header():
    assert parse_list_csv("A.US\nB.US\n") == {"tickers": ["A.US", "B.US"]}


# ---- Wikipedia S&P 500 -------------------------------------------------------------


def test_registry_knows_the_wikipedia_source():
    assert "wikipedia_sp500" in index_source_ids()
    assert isinstance(build_index_source("wikipedia_sp500"), WikipediaSp500Source)


def test_parse_sp500_page():
    history, dropped = parse_sp500_page(FIXTURE.read_text(encoding="utf-8"), as_of=date(2025, 1, 1))
    assert history.index_id == "sp500"
    assert history.as_of == date(2025, 1, 1)
    # dotted share classes map to the canonical dash form
    assert history.constituents == ("MMM.US", "AAPL.US", "BRK-B.US", "NEWCO.US")
    assert set(history.changes) == {
        IndexChange("NEWCO.US", date(2024, 6, 24), "add"),
        IndexChange("OLDCO.US", date(2024, 6, 24), "remove"),
        IndexChange("BRK-B.US", date(2019, 3, 18), "add"),
        IndexChange("GONE.US", date(2019, 3, 18), "remove"),
        # the date cell spans two rows
        IndexChange("DEAD.US", date(2019, 3, 18), "remove"),
    }
    assert dropped == 1
    spans, warnings = spans_from_index_history(history, start_date=date(2018, 1, 1))
    assert warnings == []
    by = {s.ticker: (s.start_date, s.end_date) for s in spans}
    assert by["DEAD.US"] == (date(2018, 1, 1), date(2019, 3, 18))
    assert by["NEWCO.US"] == (date(2024, 6, 24), None)


class _Session:
    def __init__(self, text: str, status: int = 200) -> None:
        self.text = text
        self.status = status
        self.urls: list[str] = []

    def get(self, url, headers=None, timeout=None):
        self.urls.append(url)
        outer = self

        class _R:
            status_code = outer.status
            text = outer.text

            def raise_for_status(self):
                if outer.status >= 400:
                    raise RuntimeError(f"HTTP {outer.status}")

        return _R()


def test_fetch_uses_the_session_and_rejects_other_indexes():
    session = _Session(FIXTURE.read_text(encoding="utf-8"))
    source = WikipediaSp500Source(session=session)
    history = source.fetch("sp500")
    assert "AAPL.US" in history.constituents
    assert session.urls and "S%26P_500" in session.urls[0]
    with pytest.raises(IndexSourceError):
        source.fetch("nasdaq100")


def test_fetch_fails_loudly_when_the_page_changes():
    source = WikipediaSp500Source(session=_Session("<html><body>nothing</body></html>"))
    with pytest.raises(IndexSourceError, match="constituents"):
        source.fetch("sp500")


def test_refresh_an_index_universe_from_the_adapter(tmp_path):
    from stonks.store.lake import DuckDBLake
    from stonks.universes import UniverseDefinition, UniverseStore, refresh_universe

    lake = DuckDBLake(tmp_path / "lake.duckdb")
    lake.migrate()
    try:
        store = UniverseStore(lake)
        store.save(
            UniverseDefinition(
                id="sp500",
                kind="index",
                spec={"index_id": "sp500", "source": "wikipedia_sp500", "start_date": "2018-01-01"},
            )
        )
        source = WikipediaSp500Source(session=_Session(FIXTURE.read_text(encoding="utf-8")))
        result = refresh_universe(
            lake, "sp500", as_of=date(2025, 1, 1), index_source_factory=lambda _sid: source
        )
        assert result.members == 7
        assert lake.members_as_of("sp500", date(2019, 1, 1)) == [
            "AAPL.US",
            "DEAD.US",
            "GONE.US",
            "MMM.US",
            "OLDCO.US",
        ]
        # the fetched history is stored, so a later refresh can run offline
        assert store.index_history("sp500") is not None
    finally:
        lake.close()

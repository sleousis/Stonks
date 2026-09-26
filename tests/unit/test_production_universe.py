"""The tick's universe resolver: a fixed list or a stored universe id."""

from __future__ import annotations

from datetime import date

import pytest

from stonks.production.universe import resolve_tick_universe
from stonks.universes import UniverseDefinition, UniverseStore, refresh_universe


@pytest.fixture
def lake(tmp_path):
    from stonks.store.lake import DuckDBLake

    lk = DuckDBLake(tmp_path / "lake.duckdb")
    lk.migrate()
    yield lk
    lk.close()


def test_a_list_is_returned_as_given(lake):
    assert resolve_tick_universe(lake, ["B.US", "A.US", "B.US"], date(2025, 1, 1)) == [
        "B.US",
        "A.US",
    ]


def test_a_universe_id_gives_members_on_the_day(lake):
    UniverseStore(lake).save(
        UniverseDefinition(
            id="u",
            kind="list",
            spec={
                "tickers": ["A.US"],
                "spans": [
                    {"ticker": "OLD.US", "start_date": "2010-01-01", "end_date": "2020-01-01"}
                ],
            },
        )
    )
    refresh_universe(lake, "u", as_of=date(2025, 1, 1))
    assert resolve_tick_universe(lake, "u", date(2019, 6, 1)) == ["A.US", "OLD.US"]
    assert resolve_tick_universe(lake, "u", date(2025, 1, 1)) == ["A.US"]


def test_an_unknown_universe_id_raises(lake):
    with pytest.raises(KeyError):
        resolve_tick_universe(lake, "missing", date(2025, 1, 1))

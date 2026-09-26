"""The tick's universe resolver: a fixed list or a stored universe id."""

from __future__ import annotations

from datetime import date

import pytest

from stonks.production.universe import (
    EmptyUniverseError,
    production_tickers,
    resolve_tick_universe,
    window_tickers,
)
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


def test_production_tickers_prefers_explicit_tickers(lake):
    assert production_tickers(lake, "missing", date(2025, 1, 1), tickers=["X.US"]) == ["X.US"]


def test_production_tickers_keeps_a_list(lake):
    assert production_tickers(lake, ["A.US", "A.US", "B.US"], date(2025, 1, 1)) == [
        "A.US",
        "B.US",
    ]


def test_production_tickers_resolves_a_universe_id_on_the_day(lake):
    UniverseStore(lake).save(UniverseDefinition(id="u", kind="list", spec={"tickers": ["A.US"]}))
    refresh_universe(lake, "u", as_of=date(2025, 1, 1))
    assert production_tickers(lake, "u", date(2025, 1, 1)) == ["A.US"]


def test_production_tickers_explains_an_empty_or_unknown_universe(lake):
    with pytest.raises(EmptyUniverseError, match="refresh"):
        production_tickers(lake, "missing", date(2025, 1, 1))
    with pytest.raises(EmptyUniverseError, match="empty"):
        production_tickers(lake, [], date(2025, 1, 1))


def test_window_tickers_takes_every_member_of_the_window(lake):
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
    assert window_tickers(lake, "u", date(2019, 1, 1), date(2025, 1, 1)) == ["A.US", "OLD.US"]
    assert window_tickers(lake, ["B.US", "B.US"], date(2019, 1, 1), date(2025, 1, 1)) == ["B.US"]
    with pytest.raises(EmptyUniverseError):
        window_tickers(lake, "missing", date(2019, 1, 1), date(2025, 1, 1))

"""Universe definitions as stored objects and their refresh (roadmap 10.5)."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from stonks.ingest.pipeline import _rows_to_df
from stonks.ingest.schemas import SymbolListing, TickerProfile
from stonks.universes import (
    IndexChange,
    IndexHistory,
    MembershipSpan,
    UniverseDefinition,
    UniverseStore,
    provider_for,
    provider_kinds,
    refresh_universe,
)
from stonks.universes.providers.index import spans_from_index_history
from tests.fixtures.universes import FakeListingSource, seed_daily_bars


@pytest.fixture
def lake(tmp_path):
    from stonks.store.lake import DuckDBLake

    lk = DuckDBLake(tmp_path / "lake.duckdb")
    lk.migrate()
    yield lk
    lk.close()


# ---- registry and store ------------------------------------------------------


def test_every_kind_has_a_provider():
    assert set(provider_kinds()) == {"list", "exchange", "rule", "index"}
    for kind in provider_kinds():
        assert provider_for(kind).kind == kind


def test_unknown_kind_is_refused():
    with pytest.raises(KeyError):
        provider_for("nope")


def test_invalid_spec_is_refused():
    with pytest.raises(ValueError):
        UniverseDefinition(id="u", kind="list", spec={}).validated()
    with pytest.raises(ValueError):
        UniverseDefinition(id="Bad Id!", kind="list", spec={"tickers": ["A.US"]})


def test_store_round_trip_keeps_created_at(lake):
    store = UniverseStore(lake)
    store.save(UniverseDefinition(id="tech", kind="list", spec={"tickers": ["A.US"]}))
    first = store.get("tech")
    assert first.created_at is not None and first.refreshed_at is None
    store.save(
        UniverseDefinition(id="tech", kind="list", name="Tech", spec={"tickers": ["A.US", "B.US"]})
    )
    second = store.get("tech")
    assert second.name == "Tech"
    assert second.spec == {"tickers": ["A.US", "B.US"]}
    assert second.created_at == first.created_at
    assert [d.id for d in store.list()] == ["tech"]
    with pytest.raises(KeyError):
        store.get("missing")


def test_delete_removes_definition_and_membership(lake):
    store = UniverseStore(lake)
    store.save(UniverseDefinition(id="tech", kind="list", spec={"tickers": ["A.US"]}))
    refresh_universe(lake, "tech", as_of=date(2025, 1, 1))
    assert lake.universe_ids() == ["tech"]
    store.delete("tech")
    assert store.list() == []
    assert lake.universe_ids() == []


# ---- list ----------------------------------------------------------------------


def test_list_refresh_is_idempotent(lake):
    store = UniverseStore(lake)
    store.save(
        UniverseDefinition(
            id="mine",
            kind="list",
            spec={
                "tickers": ["A.US", "B.US"],
                "spans": [
                    {"ticker": "OLD.US", "start_date": "2010-01-01", "end_date": "2015-01-01"}
                ],
            },
        )
    )
    first = refresh_universe(lake, "mine", as_of=date(2025, 1, 1))
    second = refresh_universe(lake, "mine", as_of=date(2025, 1, 1))
    assert first.members == second.members == 3
    assert lake.count_rows("universe_membership") == 3
    assert lake.members_as_of("mine", date(2012, 1, 1)) == ["A.US", "B.US", "OLD.US"]
    assert lake.members_as_of("mine", date(2020, 1, 1)) == ["A.US", "B.US"]
    assert store.get("mine").member_count == 3
    assert store.get("mine").refreshed_at is not None


def test_list_refresh_drops_removed_tickers(lake):
    store = UniverseStore(lake)
    store.save(UniverseDefinition(id="mine", kind="list", spec={"tickers": ["A.US", "B.US"]}))
    refresh_universe(lake, "mine", as_of=date(2025, 1, 1))
    store.save(UniverseDefinition(id="mine", kind="list", spec={"tickers": ["A.US"]}))
    refresh_universe(lake, "mine", as_of=date(2025, 1, 1))
    assert lake.members_as_of("mine", date(2024, 1, 1)) == ["A.US"]


# ---- index ----------------------------------------------------------------------


def _history() -> IndexHistory:
    return IndexHistory(
        index_id="toy",
        as_of=date(2024, 1, 1),
        constituents=("A.US", "B.US", "C.US"),
        changes=(
            # C replaced D in 2020
            IndexChange("C.US", date(2020, 6, 1), "add"),
            IndexChange("D.US", date(2020, 6, 1), "remove"),
            # B joined in 2018
            IndexChange("B.US", date(2018, 3, 1), "add"),
            # E left in 2016
            IndexChange("E.US", date(2016, 1, 4), "remove"),
        ),
        source="test",
    )


def test_index_spans_rebuild_membership_backwards():
    spans, warnings = spans_from_index_history(_history(), start_date=date(2015, 1, 1))
    assert warnings == []
    by = {(s.ticker, s.start_date, s.end_date) for s in spans}
    assert by == {
        ("A.US", date(2015, 1, 1), None),
        ("B.US", date(2018, 3, 1), None),
        ("C.US", date(2020, 6, 1), None),
        ("D.US", date(2015, 1, 1), date(2020, 6, 1)),
        ("E.US", date(2015, 1, 1), date(2016, 1, 4)),
    }


def test_index_changes_after_the_snapshot_apply_forward():
    history = IndexHistory(
        index_id="toy",
        as_of=date(2024, 1, 1),
        constituents=("A.US", "B.US"),
        changes=(
            IndexChange("B.US", date(2024, 6, 1), "remove"),
            IndexChange("N.US", date(2024, 6, 1), "add"),
        ),
        source="test",
    )
    spans, _ = spans_from_index_history(history, start_date=date(2020, 1, 1))
    by = {(s.ticker, s.start_date, s.end_date) for s in spans}
    assert by == {
        ("A.US", date(2020, 1, 1), None),
        ("B.US", date(2020, 1, 1), date(2024, 6, 1)),
        ("N.US", date(2024, 6, 1), None),
    }


def test_index_inconsistent_changes_warn():
    history = IndexHistory(
        index_id="toy",
        as_of=date(2024, 1, 1),
        constituents=("A.US",),
        # Z was added but is not a member now and never removed
        changes=(IndexChange("Z.US", date(2020, 1, 1), "add"),),
        source="test",
    )
    spans, warnings = spans_from_index_history(history, start_date=date(2019, 1, 1))
    assert {s.ticker for s in spans} == {"A.US"}
    assert warnings and "Z.US" in warnings[0]


def test_index_refresh_from_stored_history(lake):
    store = UniverseStore(lake)
    store.save_index_history(_history())
    store.save(UniverseDefinition(id="toy_idx", kind="index", spec={"index_id": "toy"}))
    result = refresh_universe(lake, "toy_idx", as_of=date(2025, 1, 1))
    # without a start_date the earliest change date starts the history, so
    # nothing is claimed before it and E (removed that day) never shows
    assert lake.members_as_of("toy_idx", date(2016, 1, 3)) == []
    assert lake.members_as_of("toy_idx", date(2016, 1, 4)) == ["A.US", "D.US"]
    assert lake.members_as_of("toy_idx", date(2021, 1, 1)) == ["A.US", "B.US", "C.US"]
    assert result.members == 4
    assert result.warnings == []
    assert store.index_history("toy") == _history()


def test_index_refresh_without_history_fails(lake):
    store = UniverseStore(lake)
    store.save(UniverseDefinition(id="x", kind="index", spec={"index_id": "none"}))
    with pytest.raises(ValueError, match="no constituent history"):
        refresh_universe(lake, "x", as_of=date(2025, 1, 1))


# ---- rule ------------------------------------------------------------------------


def test_rule_refresh_evaluates_each_rebalance_date(lake):
    # LIQ trades 1M dollars a day all year; ILLIQ only from April on
    seed_daily_bars(lake, "LIQ.US", date(2024, 1, 1), date(2024, 6, 30), close=10.0, volume=100_000)
    seed_daily_bars(lake, "ILLIQ.US", date(2024, 1, 1), date(2024, 3, 31), close=10.0, volume=10)
    seed_daily_bars(
        lake, "ILLIQ.US", date(2024, 4, 1), date(2024, 6, 30), close=10.0, volume=100_000
    )
    seed_daily_bars(lake, "PENNY.US", date(2024, 1, 1), date(2024, 6, 30), close=0.5, volume=10**8)
    lake.upsert_instrument_profile(
        _rows_to_df(
            [
                TickerProfile(id="LIQ.US", exchange="NYSE", sector="Tech"),
                TickerProfile(id="ILLIQ.US", exchange="NYSE", sector="Tech"),
                TickerProfile(id="PENNY.US", exchange="NYSE", sector="Tech"),
            ]
        )
    )
    store = UniverseStore(lake)
    store.save(
        UniverseDefinition(
            id="liquid",
            kind="rule",
            spec={
                "min_adv": 500_000,
                "min_price": 1.0,
                "sectors": ["Tech"],
                "exchanges": ["NYSE"],
                "start": "2024-02-01",
                "end": "2024-06-30",
                "rebalance": "monthly",
            },
        )
    )
    refresh_universe(lake, "liquid", as_of=date(2024, 7, 1))
    spans = lake.get_universe_membership("liquid")
    rows = {(r.ticker, r.start_date, r.end_date) for r in spans.itertuples(index=False)}
    assert ("LIQ.US", date(2024, 2, 1), date(2024, 7, 1)) in rows
    # ILLIQ enters at the first rebalance after its volume rose (May 1:
    # the 20-bar window on April 1 is still mostly thin)
    illiq = [r for r in rows if r[0] == "ILLIQ.US"]
    assert illiq == [("ILLIQ.US", date(2024, 5, 1), date(2024, 7, 1))]
    assert not any(r[0] == "PENNY.US" for r in rows)


def test_rule_spec_rejects_unknown_keys():
    with pytest.raises(ValueError):
        UniverseDefinition(
            id="r", kind="rule", spec={"start": "2024-01-01", "min_adv_typo": 1}
        ).validated()


# ---- exchange --------------------------------------------------------------------


def test_exchange_refresh_turns_listings_into_spans(lake):
    source = FakeListingSource(
        [
            SymbolListing(ticker="AAA.US", exchange="NYSE", security_type="common_stock"),
            SymbolListing(ticker="ETF.US", exchange="NYSE", security_type="etf"),
            SymbolListing(
                ticker="DEAD.US", exchange="NYSE", security_type="common_stock", is_delisted=True
            ),
            SymbolListing(
                ticker="GHOST.US", exchange="NYSE", security_type="common_stock", is_delisted=True
            ),
        ]
    )
    lake.upsert_instrument_profile(
        _rows_to_df([TickerProfile(id="AAA.US", ipo_date=date(2012, 5, 1))])
    )
    seed_daily_bars(lake, "DEAD.US", date(2019, 1, 1), date(2019, 12, 31), close=5.0, volume=1000)
    store = UniverseStore(lake)
    store.save(
        UniverseDefinition(
            id="us_common",
            kind="exchange",
            spec={"exchange": "US", "security_types": ["common_stock"], "start_date": "2000-01-01"},
        )
    )
    result = refresh_universe(
        lake, "us_common", as_of=date(2025, 1, 1), source_factory=lambda _sid: source
    )
    spans = lake.get_universe_membership("us_common")
    rows = {(r.ticker, r.start_date, r.end_date) for r in spans.itertuples(index=False)}
    assert ("AAA.US", date(2012, 5, 1), None) in rows
    # listing starts at the first bar, ends the day after the last bar
    dead = next(r for r in rows if r[0] == "DEAD.US")
    assert dead[1] == date(2019, 1, 1)
    assert dead[2] == date(2020, 1, 1)
    # a delisted name with no dates at all is kept up to the refresh date and flagged
    assert ("GHOST.US", date(2000, 1, 1), date(2025, 1, 1)) in rows
    assert any("GHOST.US" in w for w in result.warnings)
    assert not any(r[0] == "ETF.US" for r in rows)
    # the listings reach the instruments table (asset class, delisted flag)
    assert lake.delisted_tickers(["AAA.US", "DEAD.US", "GHOST.US"]) == ["DEAD.US", "GHOST.US"]
    assert source.calls == ["US"]


def test_exchange_refresh_can_skip_delisted(lake):
    source = FakeListingSource(
        [
            SymbolListing(ticker="AAA.US"),
            SymbolListing(ticker="DEAD.US", is_delisted=True),
        ]
    )
    store = UniverseStore(lake)
    store.save(
        UniverseDefinition(
            id="alive", kind="exchange", spec={"exchange": "US", "include_delisted": False}
        )
    )
    refresh_universe(lake, "alive", as_of=date(2025, 1, 1), source_factory=lambda _sid: source)
    assert lake.members_as_of("alive", date(2024, 1, 1)) == ["AAA.US"]


def test_exchange_refresh_needs_a_source(lake):
    store = UniverseStore(lake)
    store.save(UniverseDefinition(id="u", kind="exchange", spec={"exchange": "US"}))
    with pytest.raises(ValueError, match="data source"):
        refresh_universe(lake, "u", as_of=date(2025, 1, 1))


def test_membership_span_rejects_backwards_dates():
    with pytest.raises(ValueError):
        MembershipSpan("A.US", date(2020, 1, 1), date(2019, 1, 1))


def test_refresh_result_counts_current_members(lake):
    store = UniverseStore(lake)
    store.save(
        UniverseDefinition(
            id="m",
            kind="list",
            spec={
                "tickers": ["A.US"],
                "spans": [{"ticker": "B.US", "start_date": "2010-01-01", "end_date": "2011-01-01"}],
            },
        )
    )
    result = refresh_universe(lake, "m", as_of=date(2025, 1, 1))
    assert result.members == 2
    assert result.current_members == 1
    frame = lake.get_universe_membership("m")
    assert isinstance(frame, pd.DataFrame) and len(frame) == 2


# ---- edge cases (review 18.1) -------------------------------------------------------


def _member_on(spans, ticker, day):
    return any(
        s.ticker == ticker and s.start_date <= day and (s.end_date is None or day < s.end_date)
        for s in spans
    )


def test_index_remove_and_readd_on_the_same_day_keeps_the_name_a_member():
    history = IndexHistory(
        index_id="toy",
        as_of=date(2024, 1, 1),
        constituents=("A.US",),
        changes=(
            IndexChange("A.US", date(2020, 6, 1), "remove"),
            IndexChange("A.US", date(2020, 6, 1), "add"),
        ),
        source="test",
    )
    spans, warnings = spans_from_index_history(history, start_date=date(2019, 1, 1))
    assert warnings == []
    for day in (date(2019, 1, 1), date(2020, 5, 31), date(2020, 6, 1), date(2023, 12, 31)):
        assert _member_on(spans, "A.US", day)


def test_index_change_dated_on_the_snapshot_day_is_already_in_the_snapshot():
    # the snapshot is taken after that day's changes: N joined, B left
    history = IndexHistory(
        index_id="toy",
        as_of=date(2024, 1, 1),
        constituents=("A.US", "N.US"),
        changes=(
            IndexChange("N.US", date(2024, 1, 1), "add"),
            IndexChange("B.US", date(2024, 1, 1), "remove"),
        ),
        source="test",
    )
    spans, warnings = spans_from_index_history(history, start_date=date(2023, 1, 1))
    assert warnings == []
    by = {(s.ticker, s.start_date, s.end_date) for s in spans}
    assert by == {
        ("A.US", date(2023, 1, 1), None),
        ("N.US", date(2024, 1, 1), None),
        ("B.US", date(2023, 1, 1), date(2024, 1, 1)),
    }


def test_quarterly_rebalance_from_the_middle_of_a_quarter():
    from stonks.universes.providers.rule import rebalance_dates

    assert rebalance_dates(date(2024, 2, 15), date(2024, 8, 10), "quarterly") == [
        date(2024, 2, 15),
        date(2024, 4, 1),
        date(2024, 7, 1),
        date(2024, 8, 10),
    ]
    # the window ends before the first quarter boundary
    assert rebalance_dates(date(2024, 2, 15), date(2024, 3, 20), "quarterly") == [
        date(2024, 2, 15),
        date(2024, 3, 20),
    ]
    assert rebalance_dates(date(2024, 2, 15), date(2024, 2, 15), "quarterly") == [date(2024, 2, 15)]
    # a start on a boundary is not listed twice; the year rolls over
    assert rebalance_dates(date(2024, 10, 1), date(2025, 1, 1), "quarterly") == [
        date(2024, 10, 1),
        date(2025, 1, 1),
    ]


def test_exchange_symbol_delisted_before_its_ipo_is_skipped_with_a_warning(lake):
    source = FakeListingSource(
        [SymbolListing(ticker="AAA.US"), SymbolListing(ticker="ODD.US", is_delisted=True)]
    )
    lake.upsert_instrument_profile(
        _rows_to_df(
            [TickerProfile(id="ODD.US", ipo_date=date(2015, 1, 1), delisted_date=date(2010, 1, 1))]
        )
    )
    UniverseStore(lake).save(UniverseDefinition(id="x", kind="exchange", spec={"exchange": "US"}))
    result = refresh_universe(lake, "x", as_of=date(2025, 1, 1), source_factory=lambda _s: source)
    tickers = set(lake.get_universe_membership("x")["ticker"])
    assert tickers == {"AAA.US"}
    assert any("ODD.US" in w and "before they listed" in w for w in result.warnings)

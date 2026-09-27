"""The point-in-time lake proxy (BL-49, ``store/pit.py``).

Every read through ``PointInTimeLake`` is clamped to what was known at the
decision: bars by the bar-visibility rule, statements by filing date,
macro prints by publication date, dated metadata by its day, and universe
membership by its start (an exit after the decision is unknown). Raw SQL
and writes are refused.
"""

from __future__ import annotations

import copy
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from stonks.core.interval import Interval
from stonks.store.lake import DuckDBLake
from stonks.store.pit import PitSession, PointInTimeLake, PointInTimeViolation

DAYS = pd.bdate_range("2024-01-01", periods=30)
D = DAYS[20].to_pydatetime()  # the decision day
NEXT = DAYS[21].date()


@pytest.fixture(scope="module")
def lake():
    lake = DuckDBLake(Path(":memory:"))
    lake.migrate()
    closes = [100.0 + i for i in range(len(DAYS))]
    lake.upsert_prices(
        pd.DataFrame(
            {
                "ticker": "A.US",
                "date": [d.date() for d in DAYS],
                "open": closes,
                "high": closes,
                "low": closes,
                "close": closes,
                "adj_close": closes,
                "volume": 1_000.0,
            }
        )
    )
    hours = [D + timedelta(hours=h) for h in range(10, 16)] + [
        datetime.combine(NEXT, datetime.min.time()) + timedelta(hours=10)
    ]
    lake.upsert_bars(
        pd.DataFrame(
            {
                "ticker": "A.US",
                "timestamp": hours,
                "open": 1.0,
                "high": 1.0,
                "low": 1.0,
                "close": 1.0,
                "adj_close": 1.0,
                "volume": 1.0,
            }
        ),
        Interval.HOUR_1,
    )
    lake.upsert_prices(
        pd.DataFrame(
            {
                "ticker": ["LATE.US"],
                "date": [NEXT],
                "open": [5.0],
                "high": [5.0],
                "low": [5.0],
                "close": [5.0],
                "adj_close": [5.0],
                "volume": [1.0],
            }
        )
    )
    lake.upsert_income_statement(
        pd.DataFrame(
            {
                "ticker": "A.US",
                "period_end": [date(2023, 9, 30), date(2023, 12, 31)],
                "frequency": "Q",
                "filing_date": [date(2023, 11, 1), NEXT],
                "revenue": [10.0, 99.0],
            }
        )
    )
    lake.upsert_macro_indicators(
        pd.DataFrame(
            {
                "country_iso": "USA",
                "indicator": "cpi",
                "observation_date": [D.date() - timedelta(days=3), NEXT],
                "period": None,
                "country_name": "United States",
                "value": [1.0, 2.0],
            }
        )
    )
    lake.upsert_shares_outstanding(
        pd.DataFrame({"ticker": "A.US", "date": [D.date(), NEXT], "shares": [10.0, 20.0]})
    )
    lake.upsert_stock_splits(pd.DataFrame({"ticker": ["A.US"], "date": [NEXT], "ratio": [2.0]}))
    lake.upsert_dividends(
        pd.DataFrame(
            {
                "ticker": "A.US",
                "ex_date": [D.date(), NEXT],
                "amount": [0.5, 0.7],
                "currency": "USD",
                "pay_date": None,
                "record_date": None,
                "declaration_date": None,
            }
        )
    )
    lake.upsert_bond_yields(
        pd.DataFrame(
            {
                "ticker": "US10Y.GBOND",
                "date": [D.date(), NEXT],
                "yield_to_maturity": [4.0, 9.0],
                "clean_price": [None, None],
            }
        )
    )
    lake.upsert_defi_tvl(
        pd.DataFrame(
            {
                "chain": "eth",
                "observation_date": [D.date(), NEXT],
                "tvl_usd": [1.0, 2.0],
                "source": "t",
            }
        )
    )
    lake.upsert_universe_membership(
        pd.DataFrame(
            {
                "universe_id": "u",
                "ticker": ["A.US", "B.US", "C.US"],
                "start_date": [DAYS[0].date(), NEXT, DAYS[0].date()],
                "end_date": [None, None, NEXT],
            }
        )
    )
    lake.con.execute(
        "INSERT INTO instruments (id, asset_class, sector) VALUES ('A.US', 'equity', 'Tech')"
    )
    yield lake
    lake.close()


@pytest.fixture
def pit(lake):
    return PointInTimeLake(lake, D)


def test_bars_stop_at_the_decision_even_when_asked_for_more(pit):
    frame = pit.get_bars("A.US", Interval.DAY_1, DAYS[0], DAYS[-1])
    assert frame["timestamp"].max() == pd.Timestamp(D)
    assert len(frame) == 21
    prices = pit.get_prices("A.US", DAYS[0].date(), DAYS[-1].date())
    assert prices["date"].max() == D.date()


def test_a_daily_decision_sees_its_own_days_hourly_bars_but_not_tomorrows(lake):
    pit = PointInTimeLake(lake, D, decision_interval=Interval.DAY_1)
    frame = pit.get_bars("A.US", Interval.HOUR_1, DAYS[0], DAYS[-1])
    assert len(frame) == 6
    assert frame["timestamp"].max() < pd.Timestamp(NEXT)


def test_an_intraday_decision_hides_the_open_daily_bar(lake):
    view = PointInTimeLake(lake, D + timedelta(hours=10), decision_interval=Interval.HOUR_1)
    daily = view.get_bars("A.US", Interval.DAY_1, DAYS[0], DAYS[-1])
    assert daily["timestamp"].max() == pd.Timestamp(DAYS[19])
    hourly = view.get_bars("A.US", Interval.HOUR_1, DAYS[0], DAYS[-1])
    assert list(hourly["timestamp"]) == [pd.Timestamp(D + timedelta(hours=10))]


def test_statements_go_by_filing_date(pit, lake):
    hist = pit.get_statement_history("income_statement", "A.US")
    assert list(hist["revenue"]) == [10.0]
    assert len(lake.get_statement_history("income_statement", "A.US")) == 2
    as_of = pit.get_statements_as_of("income_statement", "A.US", DAYS[-1])
    assert list(as_of["revenue"]) == [10.0]
    raw = pit.get_income_statement("A.US")
    assert list(raw["revenue"]) == [10.0]


def test_a_filing_is_used_from_the_session_after_it(lake):
    """BE-22 (DS-06): a filing may land after the close, so a daily run
    on its filing day does not see it. The next one does."""
    on = PointInTimeLake(lake, datetime.combine(NEXT, datetime.min.time()))
    after = PointInTimeLake(lake, DAYS[22].to_pydatetime())
    for view, revenue in ((on, [10.0]), (after, [10.0, 99.0])):
        hist = view.get_statement_history("income_statement", "A.US")
        assert list(hist["revenue"]) == revenue
        assert list(view.get_income_statement("A.US")["revenue"].sort_values()) == revenue
        as_of = view.get_statements_as_of("income_statement", "A.US", DAYS[-1])
        assert sorted(as_of["revenue"]) == revenue


def test_macro_prints_go_by_publication_date(pit):
    series = pit.get_macro_series("USA", "cpi", stamped_at="period_end")
    assert list(series["value"]) == [1.0]
    later = pit.get_macro_series("USA", "cpi", as_of=DAYS[-1], stamped_at="period_end")
    assert list(later["value"]) == [1.0]


def test_dated_metadata_stops_at_the_decision_day(pit):
    assert list(pit.get_shares_outstanding("A.US")["shares"]) == [10.0]
    assert list(pit.get_dividends("A.US")["amount"]) == [0.5]
    actions = pit.get_corporate_actions(["A.US"])
    assert list(actions["kind"]) == ["dividend"]
    assert list(pit.get_bond_yields("US10Y.GBOND")["yield_to_maturity"]) == [4.0]
    assert list(pit.get_bond_yields("US10Y.GBOND", as_of=NEXT)["yield_to_maturity"]) == [4.0]
    assert list(pit.get_defi_tvl("eth", end=NEXT)["tvl_usd"]) == [1.0]


def test_membership_hides_future_joins_and_future_exits(pit):
    assert pit.members_as_of("u", DAYS[-1]) == ["A.US", "C.US"]
    assert pit.members_between("u", DAYS[0], DAYS[-1]) == ["A.US", "C.US"]
    spans = pit.get_universe_membership("u")
    assert list(spans["ticker"]) == ["A.US", "C.US"]
    # C.US leaves after the decision: that exit is not known yet
    assert spans["end_date"].isna().all()
    assert pit.universe_ids() == ["u"]


def test_names_and_static_profile_reads(pit, lake):
    assert pit.bar_tickers(Interval.DAY_1) == ["A.US"]
    assert lake.bar_tickers(Interval.DAY_1) == ["A.US", "LATE.US"]
    assert pit.get_asset_classes(["A.US"]) == {"A.US": "equity"}
    sectors = pit.instrument_sectors(["A.US"])
    assert list(sectors["sector"]) == ["Tech"]


def test_raw_sql_is_refused_unless_allowed(lake, pit):
    with pytest.raises(PointInTimeViolation):
        pit.sql("SELECT 1")
    raw = PointInTimeLake(lake, D, allow_raw=True)
    assert raw.sql("SELECT 1 AS x")["x"].tolist() == [1]


def test_writes_and_the_connection_are_not_exposed(pit):
    with pytest.raises(AttributeError, match="point-in-time"):
        _ = pit.con
    with pytest.raises(AttributeError):
        pit.upsert_bars(pd.DataFrame(), Interval.DAY_1)
    assert not hasattr(pit, "delisted_tickers")


def test_a_reader_the_lake_lacks_is_missing_too():
    class Stub:
        def get_bars(self, ticker, interval, start, end):
            return pd.DataFrame(columns=["timestamp", "close"])

    view = PointInTimeLake(Stub(), D)
    assert getattr(view, "get_defi_tvl", None) is None
    assert view.get_bars("A.US", Interval.DAY_1, DAYS[0], DAYS[-1]).empty


def test_views_of_one_session_share_full_history_reads(lake):
    calls = []
    original = lake.get_bars

    class Counting:
        def __getattr__(self, name):
            return getattr(lake, name)

        def get_bars(self, *args, **kwargs):
            calls.append(args[0])
            return original(*args, **kwargs)

    session = PitSession(Counting())
    for day in DAYS[10:15]:
        view = session.at(day.to_pydatetime())
        assert view.get_bars("A.US", Interval.DAY_1, DAYS[0], DAYS[-1])["timestamp"].max() == day
    assert calls == ["A.US"]


def test_the_proxy_and_its_session_are_never_deep_copied(pit):
    assert copy.deepcopy(pit) is pit
    assert copy.deepcopy(pit.pit_session) is pit.pit_session


def test_the_decision_properties(lake):
    view = PointInTimeLake(lake, D.date())
    assert view.as_of == D
    assert view.reach == D + timedelta(days=1)
    assert view.known_through == D.date()
    assert view.bar_cutoff(Interval.DAY_1) == D
    assert view.pit_session.lake is lake


def test_bar_caches_read_each_series_once_per_run_and_clamp_every_view(lake):
    from stonks.strategies._common import LakeBarCaches, memo_scope

    caches = LakeBarCaches()
    session = PitSession(lake)
    first = session.at(DAYS[10].to_pydatetime())
    second = session.at(D)
    one, two = caches.for_lake(first), caches.for_lake(second)
    assert caches.for_lake(first) is one  # one clamped cache per view
    assert one._series is two._series  # one underlying cache per lake
    far = DAYS[-1].to_pydatetime()
    assert one.last_close("A.US", Interval.DAY_1, far)[0] == DAYS[10]
    assert two.last_close("A.US", Interval.DAY_1, far)[0] == D
    assert two.last_n_bars("A.US", Interval.DAY_1, far, 3)["timestamp"].iloc[-1] == D
    assert len(two.last_n_closes("A.US", Interval.DAY_1, far, 50)) == 21
    between = two.bars_between("A.US", Interval.DAY_1, DAYS[0], far)
    assert between["timestamp"].max() == D
    assert memo_scope(first) is memo_scope(second) is session
    assert memo_scope(lake) is lake

"""Point-in-time universe membership (BL-37, principle P14)."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from stonks.core.interval import Interval
from stonks.lab.universe import UniverseRule, resolve, resolve_window
from stonks.store.lake import DuckDBLake


def _membership(lake: DuckDBLake) -> None:
    lake.upsert_universe_membership(
        pd.DataFrame(
            [
                # ALIVE joined in 2010 and is still a member
                {"universe_id": "sp", "ticker": "ALIVE.US", "start_date": date(2010, 1, 1)},
                # GONE left the index when it was delisted in mid 2015
                {
                    "universe_id": "sp",
                    "ticker": "GONE.US",
                    "start_date": date(2008, 1, 1),
                    "end_date": date(2015, 6, 1),
                },
                # NEW joined in 2020
                {"universe_id": "sp", "ticker": "NEW.US", "start_date": date(2020, 1, 1)},
                # BACK was a member twice
                {
                    "universe_id": "sp",
                    "ticker": "BACK.US",
                    "start_date": date(2009, 1, 1),
                    "end_date": date(2012, 1, 1),
                },
                {"universe_id": "sp", "ticker": "BACK.US", "start_date": date(2016, 1, 1)},
                {"universe_id": "other", "ticker": "X.US", "start_date": date(2000, 1, 1)},
            ]
        )
    )


def test_members_as_of_a_date(lake):
    _membership(lake)
    assert lake.members_as_of("sp", date(2011, 1, 1)) == ["ALIVE.US", "BACK.US", "GONE.US"]
    assert lake.members_as_of("sp", date(2015, 5, 31)) == ["ALIVE.US", "GONE.US"]
    # end_date is the first day the ticker is no longer a member
    assert lake.members_as_of("sp", date(2015, 6, 1)) == ["ALIVE.US"]
    assert lake.members_as_of("sp", date(2021, 1, 1)) == ["ALIVE.US", "BACK.US", "NEW.US"]
    assert lake.members_as_of("nope", date(2021, 1, 1)) == []


def test_members_over_a_date_range(lake):
    _membership(lake)
    assert lake.members_between("sp", date(2013, 1, 1), date(2016, 6, 1)) == [
        "ALIVE.US",
        "BACK.US",
        "GONE.US",
    ]
    assert lake.members_between("sp", date(2016, 1, 1), date(2019, 1, 1)) == [
        "ALIVE.US",
        "BACK.US",
    ]


def test_upsert_is_idempotent_and_updates_end_date(lake):
    _membership(lake)
    _membership(lake)
    assert lake.count_rows("universe_membership") == 6
    lake.upsert_universe_membership(
        pd.DataFrame(
            [
                {
                    "universe_id": "sp",
                    "ticker": "NEW.US",
                    "start_date": date(2020, 1, 1),
                    "end_date": date(2022, 1, 1),
                }
            ]
        )
    )
    assert "NEW.US" not in lake.members_as_of("sp", date(2023, 1, 1))
    assert lake.universe_ids() == ["other", "sp"]


def test_end_before_start_is_rejected(lake):
    with pytest.raises(ValueError, match="end_date"):
        lake.upsert_universe_membership(
            pd.DataFrame(
                [
                    {
                        "universe_id": "sp",
                        "ticker": "A.US",
                        "start_date": date(2020, 1, 1),
                        "end_date": date(2019, 1, 1),
                    }
                ]
            )
        )


def test_resolve_by_id_and_static_list(lake):
    _membership(lake)
    assert resolve(lake, "sp", date(2014, 1, 1)) == ["ALIVE.US", "GONE.US"]
    assert resolve(lake, ["B.US", "A.US", "B.US"], date(2014, 1, 1)) == ["B.US", "A.US"]
    with pytest.raises(KeyError, match="unknown universe"):
        resolve(lake, "missing", date(2014, 1, 1))


def _instruments(lake: DuckDBLake) -> None:
    lake.upsert_instrument_profile(
        pd.DataFrame(
            [
                {"id": "ALIVE.US", "asset_class": "equity", "sector": "Technology"},
                {
                    "id": "GONE.US",
                    "asset_class": "equity",
                    "sector": "Energy",
                    "is_delisted": True,
                    "delisted_date": date(2015, 6, 1),
                },
                {
                    "id": "LATE.US",
                    "asset_class": "equity",
                    "sector": "Technology",
                    "ipo_date": date(2018, 1, 1),
                },
                {"id": "BTC-USD.CC", "asset_class": "crypto"},
            ]
        )
    )


def test_rule_includes_delisted_names_before_their_delisting(lake):
    _instruments(lake)
    rule = UniverseRule(asset_classes=("equity",))
    assert resolve(lake, rule, date(2014, 1, 1)) == ["ALIVE.US", "GONE.US"]
    assert resolve(lake, rule, date(2016, 1, 1)) == ["ALIVE.US"]
    assert resolve(lake, rule, date(2019, 1, 1)) == ["ALIVE.US", "LATE.US"]


def test_rule_filters_sectors_and_accepts_a_mapping(lake):
    _instruments(lake)
    got = resolve(
        lake, {"asset_classes": ["equity"], "exclude_sectors": ["Energy"]}, date(2014, 1, 1)
    )
    assert got == ["ALIVE.US"]
    assert resolve(lake, {"asset_classes": ["crypto"]}, date(2014, 1, 1)) == ["BTC-USD.CC"]


def test_rule_min_adv_uses_only_bars_up_to_the_date(lake):
    _instruments(lake)
    days = pd.bdate_range("2014-01-01", periods=30)

    def bars(ticker, volume, close=10.0):
        return pd.DataFrame(
            {
                "ticker": ticker,
                "timestamp": days,
                "open": close,
                "high": close,
                "low": close,
                "close": close,
                "adj_close": close,
                "volume": volume,
            }
        )

    lake.upsert_bars(bars("ALIVE.US", 1000), Interval.DAY_1)
    lake.upsert_bars(bars("GONE.US", 10), Interval.DAY_1)
    rule = UniverseRule(asset_classes=("equity",), min_adv=5_000.0, adv_window_bars=20)
    assert resolve(lake, rule, days[-1].date()) == ["ALIVE.US"]
    # before any bar there is no dollar volume, so nobody qualifies
    assert resolve(lake, rule, date(2013, 12, 1)) == []


def test_resolve_window_is_the_union_over_the_window(lake):
    _membership(lake)
    assert resolve_window(lake, "sp", date(2014, 1, 1), date(2020, 6, 1)) == [
        "ALIVE.US",
        "BACK.US",
        "GONE.US",
        "NEW.US",
    ]
    assert resolve_window(lake, ["A.US"], date(2014, 1, 1), date(2020, 6, 1)) == ["A.US"]

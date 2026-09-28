"""Look-through exposure (roadmap 23.14): funds split into what they hold,
so the real weight of a name across AAPL, SPY and QQQ shows."""

from __future__ import annotations

from datetime import date, datetime

import pandas as pd
import pytest

from stonks.funds import Constituent, FundSnapshot, load_fund_snapshots
from stonks.insights import Book, Holding
from stonks.insights.lookthrough import look_through

DAY = date(2026, 9, 25)


def _snap(fund: str, *parts: tuple[str, float, str | None, str | None]) -> FundSnapshot:
    return FundSnapshot(
        fund, DAY, "fake", tuple(Constituent(h, w, h, s, c) for h, w, s, c in parts)
    )


FUNDS = {
    "SPY.US": _snap(
        "SPY.US",
        ("AAPL.US", 0.07, "Technology", "US"),
        ("JPM.US", 0.03, "Financial Services", "US"),
    ),
    "QQQ.US": _snap(
        "QQQ.US",
        ("AAPL.US", 0.09, "Technology", "US"),
        ("ASML.AS", 0.01, "Technology", "NL"),
    ),
}


def _book() -> Book:
    return Book(
        cash=1_000.0,
        holdings=(
            Holding("AAPL.US", "AAPL.US", 10, 200.0, 2_000.0, "equity", "Technology", "USD"),
            Holding("SPY.US", "SPY.US", 10, 500.0, 5_000.0, "equity", None, "USD"),
            Holding("QQQ.US", "QQQ.US", 4, 500.0, 2_000.0, "equity", None, "USD"),
        ),
    )


def test_real_apple_weight_counts_every_fund() -> None:
    lt = look_through(_book(), FUNDS, countries={"AAPL.US": "US"}, names={})
    apple = next(n for n in lt.names if n.key == "AAPL.US")
    # 2000 direct + 5000 * 7% + 2000 * 9% = 2530 of 10000
    assert apple.value == pytest.approx(2_530.0)
    assert apple.weight == pytest.approx(0.253)
    assert apple.direct_value == pytest.approx(2_000.0)
    assert apple.fund_value == pytest.approx(530.0)
    assert apple.funds == ["QQQ.US", "SPY.US"]
    assert lt.names[0].key == "AAPL.US"


def test_sectors_and_countries_split_funds_and_keep_the_unlisted_rest() -> None:
    lt = look_through(_book(), FUNDS, countries={"AAPL.US": "US"}, names={})
    sector = {s.key: s for s in lt.sector}
    assert sector["Technology"].value == pytest.approx(2_000 + 350 + 180 + 20)
    assert sector["Financial Services"].value == pytest.approx(150.0)
    assert sector["cash"].value == pytest.approx(1_000.0)
    # SPY lists 10 %, QQQ 10 %: the rest of each is "not listed".
    assert sector["not listed"].value == pytest.approx(4_500 + 1_800)
    assert sum(s.value for s in lt.sector) == pytest.approx(10_000.0)
    country = {s.key: s for s in lt.country}
    assert country["US"].value == pytest.approx(2_000 + 500 + 180)
    assert country["NL"].value == pytest.approx(20.0)
    assert sum(s.value for s in lt.country) == pytest.approx(10_000.0)
    assert lt.fund_value == pytest.approx(7_000.0)
    assert lt.listed_fund_value == pytest.approx(700.0)
    assert [f.fund for f in lt.funds] == ["QQQ.US", "SPY.US"]
    assert lt.funds[1].covered == pytest.approx(0.10)


def test_a_book_without_funds_matches_its_allocation() -> None:
    book = Book(
        cash=0.0,
        holdings=(Holding("AAPL.US", "AAPL.US", 1, 100.0, 100.0, "equity", "Technology"),),
    )
    lt = look_through(book, {}, countries={}, names={"AAPL.US": "Apple Inc"})
    assert [(s.key, s.weight) for s in lt.sector] == [("Technology", 1.0)]
    assert [(s.key, s.weight) for s in lt.country] == [("unknown", 1.0)]
    assert lt.names[0].name == "Apple Inc"
    assert lt.funds == []


def test_short_fund_counts_negative_and_top_limits_names() -> None:
    book = Book(
        cash=10_000.0,
        holdings=(Holding("SPY.US", "SPY.US", -2, 500.0, -1_000.0),),
    )
    lt = look_through(book, FUNDS, countries={}, names={}, top=1)
    assert len(lt.names) == 1
    assert lt.names[0].value == pytest.approx(-70.0)


def test_empty_book_has_no_weights() -> None:
    lt = look_through(Book(cash=0.0, holdings=()), FUNDS, countries={}, names={})
    assert lt.sector == [] and lt.names == []


def test_loader_reads_the_lake_and_prefers_instrument_labels(lake) -> None:
    lake.upsert_fund_holdings(
        pd.DataFrame(
            [
                {
                    "fund": "SPY.US",
                    "holding": "AAPL.US",
                    "as_of": DAY,
                    "source": "fake",
                    "weight": 0.07,
                    "sector": "Tech",
                    "country": "US",
                },
                {
                    "fund": "SPY.US",
                    "holding": "JPM.US",
                    "as_of": DAY,
                    "source": "fake",
                    "weight": 0.03,
                    "sector": "Financial Services",
                    "country": "US",
                },
            ]
        ),
        known_at=datetime(2026, 9, 26),
    )
    lake.con.execute(
        "INSERT INTO instruments (id, asset_class, sector, country_iso)"
        " VALUES ('AAPL.US', 'equity', 'Technology', 'us')"
    )
    snaps = load_fund_snapshots(lake, ["SPY.US", "AAPL.US"], date(2026, 9, 28))
    assert list(snaps) == ["SPY.US"]
    snap = snaps["SPY.US"]
    assert snap.as_of == DAY
    aapl = snap.constituents[0]
    assert (aapl.holding, aapl.sector, aapl.country) == ("AAPL.US", "Technology", "US")
    assert snap.sector_weights() == pytest.approx({"Technology": 0.07, "Financial Services": 0.03})
    assert snap.covered == pytest.approx(0.10)
    assert load_fund_snapshots(lake, [], DAY) == {}


def test_a_fund_list_over_one_hundred_percent_never_adds_up_past_the_book() -> None:
    # Vendor weights are rounded, so a full list can add up to 100.5 %. The
    # fund still counts once: its parts are scaled to its own value.
    funds = {
        "SPY.US": _snap(
            "SPY.US",
            ("AAPL.US", 0.605, "Technology", "US"),
            ("JPM.US", 0.40, "Financial Services", "US"),
        )
    }
    book = Book(
        cash=0.0,
        holdings=(Holding("SPY.US", "SPY.US", 10, 1_000.0, 10_000.0, "equity", None, "USD"),),
    )
    lt = look_through(book, funds, countries={}, names={})
    assert sum(s.value for s in lt.sector) == pytest.approx(10_000.0)
    assert sum(s.value for s in lt.country) == pytest.approx(10_000.0)
    assert sum(n.value for n in lt.names) == pytest.approx(10_000.0)
    assert lt.listed_fund_value == pytest.approx(10_000.0)

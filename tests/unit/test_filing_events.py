"""FilingEvents (roadmap 23.13): 8-K items as events for the event study,
read by acceptance time (P12)."""

from __future__ import annotations

from datetime import date, datetime

import pandas as pd
import pytest

from stonks.core.types import Portfolio
from stonks.lab.catalog import strategy_catalog
from stonks.store.lake import DuckDBLake
from stonks.store.pit import PointInTimeLake
from stonks.strategies.examples.filing_events import FilingEvents


def _filing(acc: str, ticker: str, known: datetime, items: str) -> dict:
    return {
        "accession_number": acc,
        "ticker": ticker,
        "issuer_cik": "0000000001",
        "form": "8-K",
        "filing_date": known.date(),
        "known_at": known,
        "items": items,
        "source": "edgar",
    }


@pytest.fixture
def lake(tmp_path):
    db = DuckDBLake(tmp_path / "lake.duckdb")
    db.migrate()
    db.upsert_corporate_filings(
        pd.DataFrame(
            [
                _filing("a1", "A.US", datetime(2026, 7, 30, 20, 30), "2.02,9.01"),
                _filing("b1", "B.US", datetime(2026, 7, 29, 14, 0), "5.02"),
            ]
        )
    )
    yield db
    db.close()


def test_catalogued():
    assert strategy_catalog()["filing_events"] is FilingEvents


def test_a_filing_is_a_pick_from_its_acceptance_for_hold_days(lake):
    s = FilingEvents({"hold_days": 3})

    def at(day: date, ticker: str = "A.US") -> float | None:
        return s.estimate_return(
            ticker, day, PointInTimeLake(lake, datetime.combine(day, datetime.min.time()))
        )

    assert at(date(2026, 7, 29)) is None  # not accepted yet
    # decided after the release, filled at the next open
    assert at(date(2026, 7, 30)) == pytest.approx(1.0)
    assert at(date(2026, 8, 2)) == pytest.approx(0.25)
    assert at(date(2026, 8, 3)) is None
    assert at(date(2026, 7, 30), "B.US") is None  # a 5.02 is not an earnings event


def test_items_are_checked(lake):
    s = FilingEvents({"items": "5.02"})
    assert s.estimate_return("B.US", date(2026, 7, 29), lake) is not None
    with pytest.raises(ValueError, match="unknown"):
        FilingEvents({"items": "9.99"}).estimate_return("A.US", date(2026, 7, 30), lake)
    assert FilingEvents({}).estimate_return("A.US", date(2026, 7, 30), object()) is None


def test_decide_buys_new_picks_equally_and_sells_the_rest():
    s = FilingEvents({})
    book = Portfolio(cash=1000.0, positions={"OLD.US": 5.0})
    orders = s.decide(
        [(1.0, "A.US"), (0.5, "B.US")], book, {"A.US": 10.0, "B.US": 20.0}, date(2026, 7, 31)
    )
    by = {(o.side, o.ticker): o.quantity for o in orders}
    assert by == {("sell", "OLD.US"): 5.0, ("buy", "A.US"): 50.0, ("buy", "B.US"): 25.0}


def test_the_engine_datetime_decision_time_scores_like_its_day(lake):
    """The backtest engine and the tick pass a datetime, not a date."""
    s = FilingEvents({"hold_days": 3})
    for day in (date(2026, 7, 30), date(2026, 8, 2)):
        stamp = datetime.combine(day, datetime.min.time())
        view = PointInTimeLake(lake, stamp)
        assert s.estimate_return("A.US", stamp, view) == s.estimate_return("A.US", day, view)

"""Reference data in the lake (migration 026): revisable rows keep their
versions with known_at (P12), holidays keep when they were first seen,
and identifiers fill instruments without clearing what is stored."""

from __future__ import annotations

from datetime import date, datetime, time

import pandas as pd

from stonks.ingest.reference_schemas import (
    ExchangeDetailsRow,
    ExchangeHolidayRow,
    InstrumentIdentifiersRow,
    IpoEventRow,
    SplitEventRow,
    TreasuryRateRow,
)
from stonks.store.reference import ReferenceStore

T1 = datetime(2026, 10, 1, 12)
T2 = datetime(2026, 10, 5, 12)
DAY = date(2024, 1, 2)


def _rate(rate: float, tenor: str = "10Y") -> TreasuryRateRow:
    return TreasuryRateRow(curve="par_nominal", tenor=tenor, observation_date=DAY, rate=rate)


def _count(lake, sql: str) -> int:
    return int(lake.con.execute(sql).fetchone()[0])


def test_a_revised_rate_is_a_new_version_and_the_old_one_stays_readable(lake):
    store = ReferenceStore(lake)
    store.upsert_treasury_rates([_rate(4.0), _rate(4.5, "2Y")], source="eodhd", known_at=T1)
    store.upsert_treasury_rates([_rate(4.0), _rate(4.5, "2Y")], source="eodhd", known_at=T2)
    assert _count(lake, "SELECT COUNT(*) FROM treasury_rates_versions") == 2  # nothing changed
    store.upsert_treasury_rates([_rate(4.1)], source="eodhd", known_at=T2)
    assert _count(lake, "SELECT COUNT(*) FROM treasury_rates_versions") == 3
    assert store.treasury_curve(DAY, "par_nominal") == {"10Y": 4.1, "2Y": 4.5}
    seen_before = store.treasury_curve(DAY, "par_nominal", known_at=datetime(2026, 10, 3))
    assert seen_before == {"10Y": 4.0, "2Y": 4.5}
    assert store.treasury_curve(DAY, "par_nominal", known_at=datetime(2026, 9, 1)) == {}


def test_exchange_details_and_holidays(lake):
    store = ReferenceStore(lake)
    row = ExchangeDetailsRow(
        exchange="LSE",
        name="London Exchange",
        operating_mics=("XLON",),
        timezone="Europe/London",
        open_local=time(8),
        close_local=time(16, 30),
        working_days=(1, 2, 3, 4, 5),
        holidays=(
            ExchangeHolidayRow(holiday_date=date(2026, 12, 25), name="Christmas", kind="official"),
            ExchangeHolidayRow(
                holiday_date=date(2026, 12, 24), kind="official", early_close=time(12, 30)
            ),
        ),
    )
    store.upsert_exchange_details([row], source="eodhd", known_at=T1)
    later = row.model_copy(update={"close_local": time(17)})
    store.upsert_exchange_details([later], source="eodhd", known_at=T2)
    close = lake.con.execute("SELECT close_local, working_days FROM exchange_details").fetchone()
    assert close == (time(17), "1,2,3,4,5")
    assert _count(lake, "SELECT COUNT(*) FROM exchange_details_versions") == 2
    holidays = store.exchange_holidays("LSE")
    assert list(pd.to_datetime(holidays["holiday_date"]).dt.date) == [
        date(2026, 12, 24),
        date(2026, 12, 25),
    ]
    # first seen stays
    assert set(pd.to_datetime(holidays["known_at"])) == {pd.Timestamp(T1)}
    assert holidays.iloc[0]["early_close"] == time(12, 30)


def test_ipos_key_on_filing_date_and_keep_each_status(lake):
    store = ReferenceStore(lake)
    expected = IpoEventRow(
        ticker="NEW.US", filing_date=date(2024, 1, 2), listing_date=date(2024, 2, 1)
    )
    expected = expected.model_copy(update={"status": "expected"})
    store.upsert_ipos([expected], source="eodhd", known_at=T1)
    priced = expected.model_copy(update={"status": "priced", "offer_price": 20.0})
    store.upsert_ipos([priced], source="eodhd", known_at=T2)
    # no dates at all: nothing to key it on
    store.upsert_ipos([IpoEventRow(ticker="X.US")], source="eodhd", known_at=T2)
    assert lake.con.execute("SELECT status, exchange FROM ipo_calendar").fetchall() == [
        ("priced", "US")
    ]
    statuses = lake.con.execute(
        "SELECT status FROM ipo_calendar_versions ORDER BY known_at"
    ).fetchall()
    assert statuses == [("expected",), ("priced",)]


def test_splits_are_stored_per_ticker_and_day(lake):
    store = ReferenceStore(lake)
    split = SplitEventRow(ticker="TBLT.US", split_date=DAY, old_shares=65, new_shares=1)
    assert store.upsert_splits([split, split], source="eodhd", known_at=T1) == 1
    assert lake.con.execute("SELECT old_shares, new_shares FROM split_calendar").fetchone() == (
        65.0,
        1.0,
    )


def test_identifiers_fill_known_instruments_and_never_clear_a_value(lake):
    lake.con.execute(
        "INSERT INTO instruments (id, asset_class, isin) VALUES ('AAPL.US', 'equity', 'OLD')"
    )
    store = ReferenceStore(lake)
    touched = store.update_identifiers(
        [
            InstrumentIdentifiersRow(ticker="AAPL.US", cusip="037833100", cik="0000320193"),
            InstrumentIdentifiersRow(ticker="NOPE.US", isin="US000"),
        ]
    )
    assert touched == 1
    row = lake.con.execute(
        "SELECT isin, cusip, cik FROM instruments WHERE id = 'AAPL.US'"
    ).fetchone()
    assert row == ("OLD", "037833100", "0000320193")
    assert _count(lake, "SELECT COUNT(*) FROM instruments") == 1

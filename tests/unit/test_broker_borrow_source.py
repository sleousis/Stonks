"""The borrow source a short book at a real broker trades with (roadmap
19.14): the broker's own locate, with the lake's ``borrow_rates`` as fees,
instead of the settings."""

from __future__ import annotations

from datetime import date

from stonks.execution.borrow import BorrowQuote, BorrowSource, LakeBorrowSource
from stonks.production.financing import broker_borrow_source

DAY = date(2026, 3, 17)


class _Lake:
    def borrow_rate(self, ticker, day, *, source=None, max_age_days=None):
        return {"fee_rate_annual": 0.2, "available_shares": 100.0} if ticker == "A.US" else None


class _Locating:
    def __init__(self) -> None:
        self.fees: BorrowSource | None = None

    def borrow_source(self, fees=None):
        self.fees = fees
        return fees


class _Plain:
    pass


def test_the_broker_locate_gets_the_lake_fees():
    broker = _Locating()
    source = broker_borrow_source(broker, _Lake())
    assert isinstance(broker.fees, LakeBorrowSource)
    assert source is broker.fees
    assert source is not None
    assert source.quote("A.US", DAY) == BorrowQuote("hard", 0.2, 100.0)


def test_a_broker_without_a_locate_gives_none():
    assert broker_borrow_source(_Plain(), _Lake()) is None
    assert broker_borrow_source(None, _Lake()) is None


def test_no_lake_means_no_fees():
    broker = _Locating()
    broker_borrow_source(broker, None)
    assert broker.fees is None


def test_a_lake_without_borrow_rates_means_no_fees():
    broker = _Locating()
    broker_borrow_source(broker, object())
    assert broker.fees is None

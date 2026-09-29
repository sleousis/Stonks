"""eToro instrument ids to our tickers and back, cached in memory."""

from __future__ import annotations

import pytest

from stonks.execution.brokers.base import UnsupportedTickerError
from stonks.execution.brokers.etoro.client import EtoroClient
from stonks.execution.brokers.etoro.instruments import InstrumentCatalog, ticker_for
from stonks.execution.brokers.etoro.settings import EtoroConfig
from tests.fakes.etoro_server import API_KEY, USER_KEY, FakeEtoro, FakeInstrument


@pytest.mark.parametrize(
    ("symbol", "kind", "exchange", "ticker"),
    [
        ("AAPL", "Stocks", "Nasdaq", "AAPL.US"),
        ("BRK.B", "Stocks", "NYSE", "BRK-B.US"),
        ("SPY", "ETF", "NYSE Arca", "SPY.US"),
        ("BARC.L", "Stocks", "London", "BARC.LSE"),
        ("SAP.DE", "Stocks", "Xetra", "SAP.XETRA"),
        ("BTC", "Crypto", "Digital Currency", "BTC-USD.CC"),
        ("EURUSD", "Currencies", "FX", None),
        ("GOLD", "Commodities", "Commodity", None),
        ("SPX500", "Indices", "Indices", None),
        ("7203.T", "Stocks", "Tokyo", None),
        ("AAPL", "Stocks", None, None),
    ],
)
def test_ticker_for_maps_covered_markets_only(symbol, kind, exchange, ticker):
    assert ticker_for(symbol, kind, exchange) == ticker


def client(fake: FakeEtoro) -> EtoroClient:
    return EtoroClient(EtoroConfig(), api_key=API_KEY, user_key=USER_KEY,
                       transport=fake.transport(), sleep=lambda s: None)  # fmt: skip


def test_ids_are_fetched_once_and_kept():
    fake = FakeEtoro()
    catalog = InstrumentCatalog()
    found = catalog.by_ids(client(fake), [1001, 1003, 1])
    assert {i: x.ticker for i, x in found.items()} == {1001: "AAPL.US", 1003: "BARC.LSE", 1: None}
    calls = len(fake.calls)
    again = catalog.by_ids(client(fake), [1001, 1003])
    assert again[1001].symbol == "AAPL"
    assert len(fake.calls) == calls  # no new request: ids never change


def test_an_unknown_id_is_kept_as_not_covered():
    fake = FakeEtoro()
    found = InstrumentCatalog().by_ids(client(fake), [999999])
    assert found[999999].ticker is None
    assert found[999999].symbol == "999999"


def test_resolve_a_ticker_to_its_instrument():
    fake = FakeEtoro()
    catalog = InstrumentCatalog()
    assert catalog.resolve(client(fake), "AAPL.US").id == 1001
    assert catalog.resolve(client(fake), "BTC-USD.CC").id == 100000
    assert catalog.resolve(client(fake), "BARC.LSE").id == 1003
    calls = len(fake.calls)
    catalog.resolve(client(fake), "AAPL.US")
    assert len(fake.calls) == calls


def test_resolve_picks_the_listing_on_the_right_exchange():
    fake = FakeEtoro()
    fake.instruments[2001] = FakeInstrument(2001, "SHEL", exchange_id=7)  # London
    fake.instruments[2002] = FakeInstrument(2002, "SHEL", exchange_id=5)  # NYSE
    assert InstrumentCatalog().resolve(client(fake), "SHEL.US").id == 2002


def test_a_ticker_eToro_does_not_list_is_unsupported():
    with pytest.raises(UnsupportedTickerError):
        InstrumentCatalog().resolve(client(FakeEtoro()), "ZZZZ.US")
    with pytest.raises(UnsupportedTickerError):
        InstrumentCatalog().resolve(client(FakeEtoro()), "EURUSD.FOREX")


def test_an_override_pins_the_id():
    fake = FakeEtoro()
    catalog = InstrumentCatalog(overrides={"MSFT.US": 1002})
    assert catalog.resolve(client(fake), "MSFT.US").id == 1002
    assert all(path != "/api/v2/market-data/instruments" or "symbols" not in path
               for _, path in fake.calls)  # fmt: skip

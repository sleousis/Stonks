"""A price ingest records a minimal profile for a ticker it has none for
(docs/without-a-broker.md).

The trading run skips a ticker whose asset class it does not know. A
price-only install (Yahoo prices, no metadata run) must still produce
signals, so every bar run that stores bars for an unknown ticker records
the id, the exchange and currency from the suffix and the asset class.
An existing profile is never touched."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pandas as pd
import pytest

from stonks.ingest.ensure import DataEnsurer, EnsureSettings
from stonks.ingest.minimal_profile import minimal_profile
from stonks.ingest.pipeline import IngestPipeline
from stonks.ingest.schemas import RawPriceBar, TickerProfile
from tests.fixtures.universes import FakeListingSource


def _bars(ticker: str) -> list[RawPriceBar]:
    days = [d.date() for d in pd.bdate_range(date(2025, 6, 2), date(2025, 6, 13))]
    return [
        RawPriceBar(
            ticker=ticker, date=d, open=10.0, high=10.1, low=9.9, close=10.0,
            adj_close=10.0, volume=1000,
        )
        for d in days
    ]  # fmt: skip


def _clock():
    return datetime(2025, 6, 21, 12, tzinfo=UTC)


def _profile(lake, ticker: str) -> dict | None:
    frame = lake.sql(
        "SELECT id, asset_class, exchange, currency, name FROM instruments WHERE id = ?",
        [ticker],
    )
    return None if frame.empty else frame.iloc[0].to_dict()


@pytest.mark.parametrize(
    ("ticker", "asset_class", "exchange", "currency"),
    [
        ("AAPL.US", "equity", "US", "USD"),
        # London lines quote in pence, pounds or dollars: no guess (scale 1)
        ("VOD.LSE", "equity", "LSE", None),
        ("CSPX.LSE", "equity", "LSE", None),
        ("SAP.XETRA", "equity", "XETRA", "EUR"),
        ("SHOP.TO", "equity", "TO", "CAD"),
        ("BTC-USD.CC", "crypto", "CC", "USD"),
        ("ETH-USD", "crypto", None, "USD"),
        ("GC.COMM", "commodity", "COMM", None),
        ("ODD.ZZZ", "equity", "ZZZ", None),
    ],
)
def test_the_minimal_profile_reads_the_ticker(ticker, asset_class, exchange, currency):
    profile = minimal_profile(ticker, source_id="yahoo")
    assert profile == TickerProfile(
        id=ticker, asset_class=asset_class, exchange=exchange, currency=currency
    )


def test_a_london_line_is_never_rescaled_on_a_guess(lake):
    """A USD line such as CSPX.LSE must not be read as pence: production
    multiplies GBX prices by 0.01, so a guessed GBX would size its orders
    100 times too large. Only a real metadata profile sets the unit."""
    from stonks.fx.units import price_scales

    source = FakeListingSource(prices={"CSPX.LSE": _bars("CSPX.LSE")})
    IngestPipeline(source, lake, clock=_clock).run_prices(
        ["CSPX.LSE"], since=date(2025, 6, 2), until=date(2025, 6, 13)
    )
    assert _profile(lake, "CSPX.LSE")["currency"] is None
    assert price_scales(lake, ["CSPX.LSE"]) == {}


def test_a_price_run_records_a_minimal_profile_for_an_unknown_ticker(lake):
    source = FakeListingSource(prices={"A.US": _bars("A.US"), "BTC-USD.CC": _bars("BTC-USD.CC")})
    IngestPipeline(source, lake, clock=_clock).run_prices(
        ["A.US", "BTC-USD.CC"], since=date(2025, 6, 2), until=date(2025, 6, 13)
    )
    assert _profile(lake, "A.US") == {
        "id": "A.US", "asset_class": "equity", "exchange": "US", "currency": "USD", "name": None,
    }  # fmt: skip
    assert _profile(lake, "BTC-USD.CC")["asset_class"] == "crypto"
    assert lake.get_asset_classes(["A.US", "BTC-USD.CC"]) == {
        "A.US": "equity",
        "BTC-USD.CC": "crypto",
    }


def test_a_price_run_never_overwrites_an_existing_profile(lake):
    lake.upsert_instrument_profile(
        pd.DataFrame(
            [{"id": "A.US", "asset_class": "bond", "exchange": "NYSE", "currency": None,
              "name": "A Corp"}]
        )
    )  # fmt: skip
    IngestPipeline(
        FakeListingSource(prices={"A.US": _bars("A.US")}), lake, clock=_clock
    ).run_prices(["A.US"], since=date(2025, 6, 2), until=date(2025, 6, 13))
    assert _profile(lake, "A.US") == {
        "id": "A.US", "asset_class": "bond", "exchange": "NYSE", "currency": None, "name": "A Corp",
    }  # fmt: skip


def test_a_run_that_stores_no_bars_records_no_profile(lake):
    IngestPipeline(FakeListingSource(prices={}), lake, clock=_clock).run_prices(
        ["NONE.US"], since=date(2025, 6, 2), until=date(2025, 6, 13)
    )
    assert _profile(lake, "NONE.US") is None


def test_the_on_demand_ensure_records_the_profile_too(lake):
    source = FakeListingSource(prices={"A.US": _bars("A.US")})
    DataEnsurer(lake, source, EnsureSettings(), today=date(2025, 6, 21)).ensure(
        ["A.US"], date(2025, 6, 2), date(2025, 6, 13)
    )
    assert _profile(lake, "A.US")["asset_class"] == "equity"

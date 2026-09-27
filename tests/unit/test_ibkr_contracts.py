"""IBKR contract resolution and the conId cache (roadmap 19.2)."""

from __future__ import annotations

from datetime import timedelta

import pandas as pd
import pytest

from stonks.core.clock import FakeClock
from stonks.execution.brokers.base import BrokerUnavailableError, UnsupportedTickerError
from stonks.execution.brokers.ibkr.contracts import (
    ContractResolver,
    InstrumentProfile,
    MemoryContractCache,
    SqliteContractCache,
    build_query,
    lake_instrument_lookup,
    split_ticker,
)
from tests.fakes.ib_gateway import AAPL, BRKB, T0, VOD, FakeIbGateway, stock


def gateway(*contracts) -> FakeIbGateway:
    gw = FakeIbGateway(contracts=contracts or (AAPL, BRKB, VOD))
    gw.connect()
    return gw


def test_split_ticker():
    assert split_ticker("BRK-B.US") == ("BRK-B", "US")
    with pytest.raises(UnsupportedTickerError):
        split_ticker("AAPL")


def test_query_maps_share_class_and_market():
    q = build_query("BRK-B.US", None, by_isin=False)
    assert (q.symbol, q.currency, q.exchange, q.primary_exchange) == ("BRK B", "USD", "SMART", None)
    lse = build_query("BT-A.LSE", None, by_isin=False)
    assert (lse.symbol, lse.currency, lse.primary_exchange) == ("BT.A", "GBP", "LSE")


def test_query_uses_the_profile():
    profile = InstrumentProfile("AAPL.US", isin="US0378331005", exchange="NASDAQ", currency="USD")
    q = build_query("AAPL.US", profile, by_isin=True)
    assert q.isin == "US0378331005"
    assert q.primary_exchange == "NASDAQ"
    # GBX (pence) is a price unit, not the contract currency
    gbx = build_query("VOD.LSE", InstrumentProfile("VOD.LSE", currency="GBX"), by_isin=True)
    assert gbx.currency == "GBP"
    assert gbx.isin is None


def test_unsupported_markets_are_refused():
    with pytest.raises(UnsupportedTickerError, match="does not trade"):
        build_query("BTC-USD.CC", None, by_isin=False)


def test_resolves_by_symbol_and_caches():
    gw = gateway()
    resolver = ContractResolver(gw, clock=FakeClock(T0))
    first = resolver.resolve("AAPL.US")
    assert first.con_id == AAPL.contract.con_id
    resolver.resolve("AAPL.US")
    assert len(gw.lookups) == 1
    spec = first.spec()
    assert spec.broker_id("ibkr") == str(AAPL.contract.con_id)
    assert (spec.tick_size, spec.currency, spec.exchange) == (0.01, "USD", "NASDAQ")
    assert resolver.ticker_for(AAPL.contract.con_id) == "AAPL.US"
    assert resolver.ticker_for(1) is None


def test_isin_is_tried_first():
    gw = gateway()
    lookup = {"AAPL.US": InstrumentProfile("AAPL.US", isin="US0378331005")}.get
    resolver = ContractResolver(gw, lookup=lookup)
    resolver.resolve("AAPL.US")
    assert gw.lookups[0].isin == "US0378331005"
    assert len(gw.lookups) == 1


def test_unknown_isin_falls_back_to_symbol():
    gw = gateway()
    lookup = {"AAPL.US": InstrumentProfile("AAPL.US", isin="XX000")}.get
    resolved = ContractResolver(gw, lookup=lookup).resolve("AAPL.US")
    assert resolved.con_id == AAPL.contract.con_id
    assert [q.isin for q in gw.lookups] == ["XX000", None]
    assert resolved.isin == "US0378331005"


def test_share_class_resolves():
    assert ContractResolver(gateway()).resolve("BRK-B.US").con_id == BRKB.contract.con_id


def test_ambiguity_is_refused():
    gw = gateway(stock(1, "DUP", primary="NYSE"), stock(2, "DUP", primary="ARCA"))
    with pytest.raises(UnsupportedTickerError, match="2 IBKR contracts"):
        ContractResolver(gw).resolve("DUP.US")


def test_primary_exchange_disagreement_is_refused():
    gw = gateway(stock(1, "ABC", primary="NYSE"))
    lookup = {"ABC.US": InstrumentProfile("ABC.US", exchange="NASDAQ")}.get
    with pytest.raises(UnsupportedTickerError, match="no IBKR contract"):
        ContractResolver(gw, lookup=lookup).resolve("ABC.US")


def test_primary_exchange_filter_picks_one_listing():
    gw = gateway(stock(1, "DUP", primary="NYSE"), stock(2, "DUP", primary="ARCA"))
    lookup = {"DUP.US": InstrumentProfile("DUP.US", exchange="NYSE ARCA")}.get
    assert ContractResolver(gw, lookup=lookup).resolve("DUP.US").con_id == 2


def test_currency_disagreement_is_refused():
    gw = gateway(stock(1, "ABC", currency="USD"))
    lookup = {"ABC.US": InstrumentProfile("ABC.US", currency="CAD")}.get
    with pytest.raises(UnsupportedTickerError):
        ContractResolver(gw, lookup=lookup).resolve("ABC.US")


def test_missing_contract_is_refused():
    with pytest.raises(UnsupportedTickerError, match="no IBKR contract"):
        ContractResolver(gateway()).resolve("ZZZZ.US")


def test_outage_during_lookup_is_unavailable():
    gw = gateway()
    gw.drop()
    with pytest.raises(BrokerUnavailableError):
        ContractResolver(gw).resolve("AAPL.US")


def test_stale_entries_are_verified_again_and_changes_logged():
    clock = FakeClock(T0)
    gw = gateway()
    resolver = ContractResolver(gw, clock=clock, max_age=timedelta(days=7))
    resolver.resolve("AAPL.US")
    clock.advance(timedelta(days=8))
    gw.contracts = [stock(42, "AAPL", isin="US0378331005")]
    assert resolver.resolve("AAPL.US").con_id == 42
    assert len(gw.lookups) == 2
    assert resolver.ticker_for(AAPL.contract.con_id) is None


def test_memory_cache_moves_a_con_id_to_its_new_ticker():
    cache = MemoryContractCache()
    gw = gateway()
    resolver = ContractResolver(gw, cache=cache)
    old = resolver.resolve("AAPL.US")
    from dataclasses import replace

    cache.put(replace(old, ticker="AAPL2.US"))
    assert cache.get("AAPL.US") is None
    assert cache.by_con_id(old.con_id).ticker == "AAPL2.US"


def test_sqlite_cache_round_trip(state):
    cache = SqliteContractCache(state)
    gw = gateway()
    resolver = ContractResolver(gw, cache=cache, clock=FakeClock(T0))
    resolved = resolver.resolve("VOD.LSE")
    again = SqliteContractCache(state).get("VOD.LSE")
    assert again == resolved
    assert again.price_magnifier == 100
    assert cache.by_con_id(VOD.contract.con_id).ticker == "VOD.LSE"
    assert cache.by_con_id(1) is None
    assert cache.get("NOPE.US") is None
    # a fresh resolver over the same table asks IBKR nothing
    ContractResolver(gw, cache=SqliteContractCache(state), clock=FakeClock(T0)).resolve("VOD.LSE")
    assert len(gw.lookups) == 1


def test_sqlite_cache_moves_a_con_id_and_updates_in_place(state):
    from dataclasses import replace

    cache = SqliteContractCache(state)
    resolved = ContractResolver(gateway(), cache=cache).resolve("AAPL.US")
    cache.put(replace(resolved, min_tick=0.005))
    assert cache.get("AAPL.US").min_tick == 0.005
    cache.put(replace(resolved, ticker="APPLE.US"))
    assert cache.get("AAPL.US") is None
    assert cache.by_con_id(resolved.con_id).ticker == "APPLE.US"


class _Lake:
    def __init__(self, df=None, fail=False):
        self.df = df
        self.fail = fail

    def sql(self, query, params=None):
        if self.fail:
            raise RuntimeError("no table")
        return self.df


def test_lake_lookup():
    df = pd.DataFrame([{"isin": "US0378331005", "exchange": "NASDAQ", "currency": None}])
    profile = lake_instrument_lookup(_Lake(df))("AAPL.US")
    assert profile == InstrumentProfile("AAPL.US", "US0378331005", "NASDAQ", None)
    assert lake_instrument_lookup(_Lake(df.iloc[0:0]))("AAPL.US") is None
    assert lake_instrument_lookup(_Lake(fail=True))("AAPL.US") is None


# ---- contracts back onto tickers (roadmap 19.3) ------------------------------------------


def test_ticker_for_symbol_undoes_the_share_class_separator():
    from stonks.execution.brokers.ibkr.contracts import ticker_for_symbol

    assert ticker_for_symbol("BRK B", "US") == "BRK-B.US"
    assert ticker_for_symbol("bt.a", "lse") == "BT-A.LSE"
    assert ticker_for_symbol("SAP", "XETRA") == "SAP.XETRA"
    assert ticker_for_symbol("7203", "TSE") is None
    assert ticker_for_symbol("", "US") is None


def test_ticker_for_contract_uses_the_primary_exchange():
    from dataclasses import replace

    from stonks.execution.brokers.ibkr.contracts import ticker_for_contract

    assert ticker_for_contract(AAPL.contract) == "AAPL.US"
    assert ticker_for_contract(BRKB.contract) == "BRK-B.US"
    assert ticker_for_contract(VOD.contract) == "VOD.LSE"
    assert ticker_for_contract(replace(AAPL.contract, primary_exchange=None)) == "AAPL.US"
    assert ticker_for_contract(replace(AAPL.contract, sec_type="OPT")) is None
    assert ticker_for_contract(stock(1, "7203", currency="JPY", primary="TSEJ").contract) is None

"""The broker export preset seam: registry, detection, and the lake's ISIN
resolver."""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from stonks.connections.statement_csv import StatementError
from stonks.connections.statement_presets import registry
from stonks.connections.statement_presets.base import Listing, NoResolver
from stonks.connections.statement_presets.lake_resolver import (
    LakeInstrumentResolver,
    pick_listing,
)


def test_the_degiro_presets_are_discovered():
    ids = {p.id for p in registry.presets()}
    assert {"degiro_transactions", "degiro_account", "degiro_portfolio"} <= ids
    for p in registry.presets():
        assert p.broker and p.label and p.how_to_export
        assert p.kind in ("activities", "holdings")
    assert registry.preset("degiro_portfolio").kind == "holdings"


def test_an_unknown_preset_is_refused():
    with pytest.raises(StatementError, match="no preset named 'nope'"):
        registry.preset("nope")


def test_detection_ignores_other_files():
    assert registry.detect(["Date", "Action", "Symbol", "Quantity"]) is None
    assert registry.detect([]) is None


def test_a_listing_is_picked_by_exchange_then_currency_then_alone():
    both = [("AAPL.US", "USD"), ("APC.XETRA", "EUR")]
    assert pick_listing(Listing("US0378331005", "US", "USD"), both) == "AAPL.US"
    assert pick_listing(Listing("US0378331005", "XETRA", None), both) == "APC.XETRA"
    assert pick_listing(Listing("US0378331005", None, "EUR"), both) == "APC.XETRA"
    # nothing tells them apart: never a guess
    assert pick_listing(Listing("US0378331005", None, "GBP"), both) is None
    assert pick_listing(Listing("US0378331005", None, None), [("AAPL.US", "USD")]) == "AAPL.US"
    assert pick_listing(Listing("US0378331005"), []) is None


def test_the_lake_resolver_reads_instrument_isins(lake):
    lake.con.execute(
        "INSERT INTO instruments (id, asset_class, exchange, currency, isin) VALUES"
        " ('AAPL.US', 'equity', 'NASDAQ', 'USD', 'US0378331005'),"
        " ('APC.XETRA', 'equity', 'XETRA', 'EUR', 'US0378331005'),"
        " ('ASML.AS', 'equity', 'AS', 'EUR', 'nl0010273215')"
    )

    @contextmanager
    def open_lake():
        yield lake

    apple = Listing("US0378331005", "US", "USD")
    asml = Listing("NL0010273215", "AS", "EUR")
    unknown = Listing("IE00B4L5Y983", None, "EUR")
    got = LakeInstrumentResolver(open_lake).resolve([apple, asml, unknown])
    assert got == {apple: "AAPL.US", asml: "ASML.AS", unknown: None}


def test_an_unreadable_lake_maps_nothing():
    @contextmanager
    def broken():
        raise OSError("the lake is held by another process")
        yield  # pragma: no cover

    listing = Listing("US0378331005", "US", "USD")
    assert LakeInstrumentResolver(broken).resolve([listing]) == {listing: None}
    assert NoResolver().resolve([listing]) == {listing: None}

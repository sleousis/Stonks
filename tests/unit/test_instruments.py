"""The general instrument model (``core/instruments.py``) and multi-leg
parent orders (``core/combos.py``)."""

from __future__ import annotations

from datetime import date

import pytest

from stonks.backtest.options_ledger import OptionLedger
from stonks.core.combos import ComboLeg, ComboOrder
from stonks.core.instruments import InstrumentBook, InstrumentSpec
from stonks.core.options import OptionContract

EXP = date(2026, 1, 16)
CALL = OptionContract("AAPL.US", EXP, 150.0, "call")


def test_spot_defaults():
    s = InstrumentSpec.spot("AAPL.US")
    assert (s.kind, s.multiplier, s.expiry, s.underlying, s.lot_size) == (
        "spot",
        1.0,
        None,
        None,
        1.0,
    )
    assert s.notional(10, 200.0) == 2_000.0
    assert not s.is_option
    with pytest.raises(ValueError):
        s.option_contract()


def test_option_round_trip_and_broker_ids():
    s = InstrumentSpec.for_option(CALL, exchange="SMART", broker_ids=[("ibkr", "123")])
    assert s.symbol == CALL.contract_id and s.kind == "option" and s.multiplier == 100.0
    assert s.option_contract() == CALL
    assert s.notional(2, 3.5) == 700.0
    assert s.broker_id("ibkr") == "123" and s.broker_id("alpaca") is None
    t = s.with_broker_id("alpaca", "x").with_broker_id("ibkr", "456")
    assert t.broker_ids == (("alpaca", "x"), ("ibkr", "456"))
    assert s.broker_ids == (("ibkr", "123"),)  # frozen, a new spec each time


@pytest.mark.parametrize(
    "kw",
    [
        {"symbol": ""},
        {"symbol": "A", "multiplier": 0.0},
        {"symbol": "A", "tick_size": float("nan")},
        {"symbol": "A", "expiry": EXP},
        {"symbol": "A", "kind": "option", "underlying": "B"},
        {"symbol": "F", "kind": "future", "underlying": "B"},
    ],
)
def test_invalid_specs(kw):
    with pytest.raises(ValueError):
        InstrumentSpec(**kw)


def test_future_spec():
    f = InstrumentSpec(
        "ES.CME:2026-03",
        "commodity",
        "future",
        multiplier=50.0,
        underlying="ES",
        expiry=date(2026, 3, 20),
    )
    assert f.notional(1, 5_000.0) == 250_000.0


def test_book_resolves_known_option_and_unknown_ids():
    book = InstrumentBook([InstrumentSpec.for_option(CALL, exchange="CBOE")])
    assert CALL.contract_id in book and len(book) == 1
    assert book.get(CALL.contract_id).exchange == "CBOE"
    adjusted = "AAPL.US:2026-01-16:C:100:150"
    assert book.multiplier(adjusted) == 150.0
    assert book.multiplier("MSFT.US") == 1.0
    book.add(InstrumentSpec.spot("MSFT.US", exchange="NASDAQ"))
    assert book.get("MSFT.US").exchange == "NASDAQ"
    value = book.value(
        {CALL.contract_id: 2, "MSFT.US": 10, "X": 1}, {CALL.contract_id: 3.0, "MSFT.US": 400.0}
    )
    assert value == 600.0 + 4_000.0


def test_combo_legs_expose_their_instrument_spec():
    combo = ComboOrder("c", (ComboLeg("buy", 100, shares="AAPL.US"), ComboLeg("sell", 1, CALL)))
    specs = [leg.spec for leg in combo.legs]
    assert [s.kind for s in specs] == ["spot", "option"]
    assert specs[1].option_contract() == CALL


def test_ledger_instrument_book():
    led = OptionLedger(cash=0.0, shares={"AAPL.US": 100})
    led.fill_option(CALL, -1, 2.0, 0.0, EXP)
    book = led.instrument_book()
    assert book.multiplier(CALL.contract_id) == 100.0 and "AAPL.US" in book
    marks = {CALL.contract_id: 2.0, "AAPL.US": 150.0}
    assert book.value(led.positions(), marks) == 100 * 150.0 - 200.0

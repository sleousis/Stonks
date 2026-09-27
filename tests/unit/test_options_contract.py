"""Option contract identity (roadmap 17.1): canonical ids, OCC symbols,
exercise by exception and split adjustments by hand."""

from __future__ import annotations

from datetime import date

import pytest

from stonks.core.options import (
    OptionContract,
    adjust_for_split,
    format_number,
    is_option_id,
    occ_symbol,
    parse_contract_id,
    parse_occ_symbol,
)

JAN = date(2026, 1, 16)


def call(strike: float = 150.0, **kw) -> OptionContract:
    return OptionContract("AAPL.US", JAN, strike, "call", **kw)


def test_contract_id_is_canonical_and_round_trips():
    c = call()
    assert c.contract_id == "AAPL.US:2026-01-16:C:150"
    assert parse_contract_id(c.contract_id) == c
    put = OptionContract("SPY.US", JAN, 452.5, "put")
    assert put.contract_id == "SPY.US:2026-01-16:P:452.5"
    assert parse_contract_id(put.contract_id) == put


def test_non_standard_multiplier_is_part_of_the_id():
    c = call(100.0, multiplier=150.0)
    assert c.contract_id == "AAPL.US:2026-01-16:C:100:150"
    assert parse_contract_id(c.contract_id).multiplier == 150.0


def test_parse_keeps_style_and_settlement_given():
    c = parse_contract_id("SPX.INDX:2026-01-16:P:4000", style="european", settlement="cash")
    assert (c.style, c.settlement, c.right, c.strike) == ("european", "cash", "put", 4000.0)


@pytest.mark.parametrize(
    "bad",
    ["AAPL.US", "AAPL.US:2026-01-16:X:150", "AAPL.US:2026-13-16:C:150", "A:2026-01-16:C:abc"],
)
def test_bad_ids_are_refused(bad):
    with pytest.raises(ValueError):
        parse_contract_id(bad)
    assert not is_option_id(bad)


def test_is_option_id():
    assert is_option_id("AAPL.US:2026-01-16:C:150")
    assert not is_option_id("AAPL.US")


@pytest.mark.parametrize(
    "kw",
    [
        {"strike": 0.0},
        {"strike": float("nan")},
        {"multiplier": 0.0},
        {"right": "straddle"},
        {"style": "bermudan"},
        {"settlement": "gold"},
        {"underlying": ""},
        {"underlying": "A:B"},
        {"expiry": "2026-01-16"},
    ],
)
def test_invalid_contracts_raise(kw):
    base = {"underlying": "AAPL.US", "expiry": JAN, "strike": 150.0, "right": "call"}
    with pytest.raises(ValueError):
        OptionContract(**{**base, **kw})


def test_occ_symbol_both_ways():
    assert call().occ_symbol() == "AAPL  260116C00150000"
    assert occ_symbol("SPY", JAN, "put", 452.5) == "SPY   260116P00452500"
    assert parse_occ_symbol("AAPL  260116C00150000") == ("AAPL", JAN, "call", 150.0)
    assert parse_occ_symbol("AAPL260116C00150000") == ("AAPL", JAN, "call", 150.0)
    assert parse_occ_symbol("BRKB  260116P00000500") == ("BRKB", JAN, "put", 0.5)


@pytest.mark.parametrize(
    "bad", ["", "AAPL", "TOOLONGROOT260116C00150000", "AAPL  26011XC00150000", "AAPL  261316C00150000"]
)
def test_bad_occ_symbols(bad):
    with pytest.raises(ValueError):
        parse_occ_symbol(bad)


def test_occ_symbol_range_checks():
    with pytest.raises(ValueError):
        occ_symbol("", JAN, "call", 1.0)
    with pytest.raises(ValueError):
        occ_symbol("AAPL", JAN, "call", 100_000.0)
    with pytest.raises(ValueError):
        occ_symbol("AAPL", JAN, "fwd", 1.0)  # type: ignore[arg-type]


def test_intrinsic_and_exercise_by_exception():
    c, p = call(100.0), OptionContract("AAPL.US", JAN, 100.0, "put")
    assert c.intrinsic(105.0) == 5.0 and c.intrinsic(95.0) == 0.0
    assert p.intrinsic(95.0) == 5.0 and p.intrinsic(105.0) == 0.0
    assert c.auto_exercises(100.01) and not c.auto_exercises(100.005)
    assert p.auto_exercises(99.99) and not p.auto_exercises(100.0)


def test_time_to_expiry():
    c = call()
    assert c.days_to_expiry(date(2026, 1, 6)) == 10
    assert c.year_fraction(date(2025, 1, 16)) == pytest.approx(365 / 365)
    assert c.year_fraction(date(2026, 2, 1)) == 0.0


def test_format_number():
    assert format_number(150.0) == "150"
    assert format_number(152.5) == "152.5"
    assert format_number(1e-7) == "0.0000001"
    with pytest.raises(ValueError):
        format_number(float("inf"))


def test_whole_forward_split_multiplies_contracts():
    new, qty = adjust_for_split(call(150.0), 3.0, 2.0)
    assert (new.strike, new.multiplier, qty) == (75.0, 100.0, 6.0)
    # the deliverable doubles with the shares, the strike value is unchanged
    assert qty * new.multiplier == 3 * 100 * 2
    assert qty * new.multiplier * new.strike == 3 * 100 * 150


def test_fractional_split_changes_the_deliverable():
    new, qty = adjust_for_split(call(150.0), -2.0, 1.5)
    assert (new.strike, new.multiplier, qty) == (100.0, 150.0, -2.0)
    assert new.contract_id == "AAPL.US:2026-01-16:C:100:150"


def test_reverse_split_changes_the_deliverable():
    new, qty = adjust_for_split(call(10.0), 1.0, 0.1)
    assert (new.strike, new.multiplier, qty) == (100.0, 10.0, 1.0)


def test_split_edge_cases():
    c = call()
    assert adjust_for_split(c, 1.0, 1.0) == (c, 1.0)
    with pytest.raises(ValueError):
        adjust_for_split(c, 1.0, 0.0)

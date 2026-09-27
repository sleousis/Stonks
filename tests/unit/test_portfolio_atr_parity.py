"""ATR risk-parity constructor (Clenow): shares = equity * risk_factor / ATR."""

from __future__ import annotations

import math
from datetime import date, timedelta

import pytest

from stonks.core.types import Portfolio
from stonks.features.sessions import week_index
from stonks.portfolio import constructor_names, get_constructor, orders_from_targets
from stonks.portfolio.atr_parity import AtrConstructionInput, AtrParity


def _resize_day() -> date:
    day = date(2024, 6, 12)
    while week_index(day) % 2 != 0:
        day += timedelta(days=7)
    return day


RESIZE_DAY = _resize_day()
HOLD_DAY = RESIZE_DAY + timedelta(days=7)


def _inp(signals, atrs, prices, *, cash=100_000.0, positions=None, as_of=RESIZE_DAY, **kw):
    return AtrConstructionInput(
        signals={"sotm": signals},
        portfolio=Portfolio(cash=cash, positions=dict(positions or {})),
        prices=prices,
        as_of=as_of,
        atrs=atrs,
        **kw,
    )


def test_registered_and_discoverable():
    assert "atr_parity" in constructor_names()
    c = get_constructor("atr_parity", risk_factor=0.002)
    assert isinstance(c, AtrParity)
    assert c.settings.risk_factor == 0.002


def test_unknown_setting_is_rejected():
    with pytest.raises(ValueError):
        get_constructor("atr_parity", nope=1)


def test_shares_by_hand():
    c = get_constructor("atr_parity")
    book = c.target_weights(_inp({"A": 1.0}, {"A": 2.0}, {"A": 50.0}))
    # shares = 100_000 * 0.001 / 2 = 50; weight = 50 * 50 / 100_000
    assert book.weights == {"A": pytest.approx(0.025)}
    orders = orders_from_targets(
        book.weights,
        Portfolio(cash=100_000.0, positions={}),
        {"A": 50.0},
        buffer_fraction=0.0,
        as_of=RESIZE_DAY,
    )
    assert [(o.ticker, o.side) for o in orders] == [("A", "buy")]
    assert orders[0].quantity == pytest.approx(50.0)


def test_buys_top_down_until_the_book_is_full():
    c = get_constructor("atr_parity", risk_factor=0.01)
    # each name wants 0.01 * 100 / 2 = 0.5 of equity
    signals = {"A": 3.0, "B": 2.0, "C": 1.0}
    atrs = dict.fromkeys(signals, 2.0)
    prices = dict.fromkeys(signals, 100.0)
    book = c.target_weights(_inp(signals, atrs, prices))
    assert book.weights == {"A": pytest.approx(0.5), "B": pytest.approx(0.5)}
    assert book.meta["unfunded"] == ["C"]
    assert book.gross <= 1.0 + 1e-12


def test_a_position_bigger_than_the_book_is_not_bought():
    c = get_constructor("atr_parity", risk_factor=0.05)
    book = c.target_weights(
        _inp({"A": 2.0, "B": 1.0}, {"A": 1.0, "B": 50.0}, {"A": 100.0, "B": 100.0})
    )
    # A wants 5x equity: unfunded; B wants 0.1
    assert book.weights == {"B": pytest.approx(0.1)}


def test_names_without_price_or_atr_or_positive_score_are_skipped():
    c = get_constructor("atr_parity")
    signals = {"A": 1.0, "B": 1.0, "C": -1.0, "D": 1.0}
    atrs = {"A": 1.0, "C": 1.0, "D": 0.0}
    prices = {"A": 10.0, "B": 10.0, "C": 10.0, "D": 10.0}
    book = c.target_weights(_inp(signals, atrs, prices))
    assert set(book.weights) == {"A"}


def test_falls_back_to_annual_vol_without_atrs():
    c = get_constructor("atr_parity")
    from stonks.portfolio.base import ConstructionInput

    inp = ConstructionInput(
        signals={"s": {"A": 1.0}},
        portfolio=Portfolio(cash=10_000.0, positions={}),
        prices={"A": 100.0},
        as_of=RESIZE_DAY,
        vols_annual={"A": 0.30},
    )
    book = c.target_weights(inp)
    daily_move = 100.0 * inp.vols_annual["A"] / math.sqrt(252)
    assert book.weights["A"] == pytest.approx(0.001 * 100.0 / daily_move)


def test_held_names_keep_their_shares_between_resize_weeks():
    c = get_constructor("atr_parity")
    # held 100 shares at 50 (5_000 of 100_000 equity); parity wants 50 shares
    inp = _inp(
        {"A": 1.0}, {"A": 2.0}, {"A": 50.0}, cash=95_000.0, positions={"A": 100.0}, as_of=HOLD_DAY
    )
    book = c.target_weights(inp)
    assert book.weights == {"A": pytest.approx(0.05)}
    assert book.meta["resize"] is False


def test_held_names_are_resized_on_resize_weeks_beyond_tolerance():
    c = get_constructor("atr_parity")
    inp = _inp({"A": 1.0}, {"A": 2.0}, {"A": 50.0}, cash=95_000.0, positions={"A": 100.0})
    book = c.target_weights(inp)
    assert book.meta["resize"] is True
    assert book.weights == {"A": pytest.approx(0.025)}


def test_small_deviation_within_tolerance_is_not_resized():
    c = get_constructor("atr_parity", resize_tolerance=0.10)
    # held 52 shares vs a 50-share target: 4% off
    inp = _inp({"A": 1.0}, {"A": 2.0}, {"A": 50.0}, cash=97_400.0, positions={"A": 52.0})
    book = c.target_weights(inp)
    assert book.weights["A"] == pytest.approx(52 * 50 / 100_000)


def test_held_names_are_funded_before_new_buys():
    c = get_constructor("atr_parity", risk_factor=0.01)
    # B is held at 0.6 of equity; A ranks higher but only 0.4 is left
    inp = _inp(
        {"A": 5.0, "B": 1.0},
        {"A": 2.0, "B": 2.0},
        {"A": 100.0, "B": 100.0},
        cash=40_000.0,
        positions={"B": 600.0},
        as_of=HOLD_DAY,
    )
    book = c.target_weights(inp)
    assert book.weights == {"B": pytest.approx(0.6)}
    assert book.meta["unfunded"] == ["A"]


def test_held_name_without_a_signal_is_dropped():
    c = get_constructor("atr_parity")
    inp = _inp({"A": 1.0}, {"A": 2.0, "B": 2.0}, {"A": 50.0, "B": 50.0}, positions={"B": 10.0})
    assert "B" not in c.target_weights(inp).weights


def test_resize_cadence_is_configurable():
    c = get_constructor("atr_parity", resize_every_weeks=1)
    inp = _inp(
        {"A": 1.0}, {"A": 2.0}, {"A": 50.0}, cash=95_000.0, positions={"A": 100.0}, as_of=HOLD_DAY
    )
    assert c.target_weights(inp).meta["resize"] is True


def test_held_weights_above_max_gross_are_scaled_down_and_buy_nothing_new():
    """Edge case: held names already exceed ``max_gross`` (cash went
    negative, or the book rallied). Hold weights shrink pro rata to the cap
    and no new name is funded."""
    c = get_constructor("atr_parity")
    inp = _inp(
        {"A": 2.0, "B": 1.5, "C": 1.0},
        {"A": 2.0, "B": 2.0, "C": 2.0},
        {"A": 50.0, "B": 50.0, "C": 50.0},
        cash=-20_000.0,
        positions={"A": 1_200.0, "B": 1_200.0},
        as_of=HOLD_DAY,
    )
    book = c.target_weights(inp)
    assert set(book.weights) == {"A", "B"}
    assert sum(book.weights.values()) == pytest.approx(1.0)
    assert book.weights["A"] == pytest.approx(book.weights["B"])
    assert book.meta["unfunded"] == ["C"]

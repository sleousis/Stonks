"""Whole-share lot sizing in the shared order pipeline (roadmap 23.1, P21)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from stonks.core.types import Order
from stonks.portfolio.lots import (
    LOT_PROFILES,
    LotSettings,
    LotStats,
    broker_lot_profile,
)


def _order(ticker: str, side: str, qty: float, effect: str | None = None) -> Order:
    return Order(
        client_id=f"c:{ticker}:{side}",
        ticker=ticker,
        side=side,  # type: ignore[arg-type]
        quantity=qty,
        position_effect=effect,  # type: ignore[arg-type]
    )


PRICES = {"AAPL.US": 200.0, "BTC-USD.CC": 60_000.0, "BRK-A.US": 600_000.0}
CLASSES = {"AAPL.US": "equity", "BTC-USD.CC": "crypto", "BRK-A.US": "equity"}


# ---- profiles ------------------------------------------------------------------------


def test_profiles_are_registered():
    assert {"fractional", "whole_shares", "whole"} <= set(LOT_PROFILES)


def test_broker_profiles():
    assert broker_lot_profile("ibkr") == "whole_shares"
    assert broker_lot_profile("alpaca") == "fractional"
    assert broker_lot_profile("simulated") is None
    # an unknown broker gets whole shares, which every broker accepts
    assert broker_lot_profile("connection") == "whole_shares"


def test_unknown_profile_is_refused():
    with pytest.raises(ValidationError):
        LotSettings(profile="lots_of_lots")
    with pytest.raises(ValidationError):
        LotSettings(lot_sizes={"X.US": 0})


def test_default_rule_is_fractional_and_changes_nothing():
    rule = LotSettings().rule()
    orders = [_order("AAPL.US", "buy", 2.7)]
    result = rule.size(orders, {}, PRICES, CLASSES)
    assert result.orders == orders
    assert result.changes == []


# ---- whole shares --------------------------------------------------------------------


def test_whole_shares_floor_equities_and_keep_crypto_fractional():
    rule = LotSettings(profile="whole_shares").rule()
    orders = [_order("AAPL.US", "buy", 2.7), _order("BTC-USD.CC", "buy", 0.034)]
    result = rule.size(orders, {}, PRICES, CLASSES)
    assert [o.quantity for o in result.orders] == [2.0, 0.034]
    (change,) = result.changes
    assert change.ticker == "AAPL.US"
    assert change.requested == 2.7 and change.sized == 2.0
    assert not change.skipped


def test_below_one_lot_is_skipped():
    rule = LotSettings(profile="whole_shares").rule()
    result = rule.size([_order("BRK-A.US", "buy", 0.4)], {}, PRICES, CLASSES)
    assert result.orders == []
    assert [c.skipped for c in result.changes] == [True]
    assert result.skipped[0].notional == pytest.approx(0.4 * 600_000.0)


def test_a_full_exit_sells_the_exact_position():
    rule = LotSettings(profile="whole_shares").rule()
    result = rule.size([_order("AAPL.US", "sell", 3.5)], {"AAPL.US": 3.5}, PRICES, CLASSES)
    assert [o.quantity for o in result.orders] == [3.5]
    assert result.changes == []


def test_a_partial_sell_floors():
    rule = LotSettings(profile="whole_shares").rule()
    result = rule.size([_order("AAPL.US", "sell", 3.5)], {"AAPL.US": 10.0}, PRICES, CLASSES)
    assert [o.quantity for o in result.orders] == [3.0]


def test_a_full_cover_keeps_its_quantity():
    rule = LotSettings(profile="whole_shares").rule()
    order = _order("AAPL.US", "buy", 2.5, "close")
    result = rule.size([order], {"AAPL.US": -2.5}, PRICES, CLASSES)
    assert result.orders == [order]


def test_float_noise_is_not_rounded_away():
    rule = LotSettings(profile="whole_shares").rule()
    result = rule.size([_order("AAPL.US", "buy", 2.9999999999)], {}, PRICES, CLASSES)
    assert [o.quantity for o in result.orders] == [3.0]


def test_dust_is_skipped_not_sent_as_zero():
    rule = LotSettings(profile="whole_shares").rule()
    result = rule.size([_order("AAPL.US", "buy", 1e-12)], {}, PRICES, CLASSES)
    assert result.orders == []
    assert len(result.skipped) == 1


def test_ticker_lot_size_override():
    rule = LotSettings(profile="whole_shares", lot_sizes={"AAPL.US": 10}).rule()
    result = rule.size([_order("AAPL.US", "buy", 27)], {}, PRICES, CLASSES)
    assert [o.quantity for o in result.orders] == [20.0]


def test_whole_profile_rounds_crypto_too():
    rule = LotSettings(profile="whole").rule()
    result = rule.size([_order("BTC-USD.CC", "buy", 1.5)], {}, PRICES, CLASSES)
    assert [o.quantity for o in result.orders] == [1.0]


def test_option_contract_ids_are_left_alone():
    rule = LotSettings(profile="whole_shares").rule()
    order = _order("AAPL.US:2026-01-16:C:150", "buy", 1.0)
    assert rule.size([order], {}, {order.ticker: 5.0}, {}).orders == [order]


def test_drift_is_the_weight_lost_to_rounding():
    rule = LotSettings(profile="whole_shares").rule()
    result = rule.size([_order("AAPL.US", "buy", 2.5)], {}, PRICES, CLASSES)
    assert result.drift(10_000.0) == pytest.approx(0.5 * 200.0 / 10_000.0)


# ---- stats and minimum capital --------------------------------------------------------


def test_stats_report_skips_drift_and_minimum_capital():
    settings = LotSettings(profile="whole_shares")
    stats = LotStats(settings)
    rule = settings.rule()
    # equity 1,000: a 2.5 share buy of a 200 stock is 50% of the book, so one
    # share needs 1,000 * 1 / 2.5 = 400 of capital
    first = [_order("AAPL.US", "buy", 2.5)]
    stats.record(first, rule.size(first, {}, PRICES, CLASSES), {}, CLASSES, 1_000.0)
    second = [_order("BRK-A.US", "buy", 0.001)]
    stats.record(second, rule.size(second, {}, PRICES, CLASSES), {}, CLASSES, 1_000.0)
    report = stats.report()
    assert report.profile == "whole_shares"
    assert report.orders == 2
    assert report.rounded == 1
    assert report.skipped == 1
    assert report.skipped_share == pytest.approx(0.5)
    assert report.max_drift == pytest.approx(0.6)  # the whole skipped order
    assert report.min_capital == pytest.approx(
        sorted([400.0, 1_000.0 / 0.001])[-1], rel=0.1
    )
    metrics = report.metrics()
    assert metrics["lot_skipped_orders"] == 1.0
    assert "min_capital" in metrics


def test_minimum_capital_is_measured_even_when_rounding_is_off():
    settings = LotSettings()  # fractional orders, measured against whole shares
    stats = LotStats(settings)
    orders = [_order("AAPL.US", "buy", 2.5)]
    stats.record(orders, settings.rule().size(orders, {}, PRICES, CLASSES), {}, CLASSES, 1_000.0)
    report = stats.report()
    assert report.skipped == 0 and report.rounded == 0
    assert report.min_capital == pytest.approx(400.0)
    assert report.min_capital_profile == "whole_shares"


def test_sells_do_not_set_the_minimum_capital():
    settings = LotSettings(profile="whole_shares")
    stats = LotStats(settings)
    orders = [_order("AAPL.US", "sell", 0.2)]
    positions = {"AAPL.US": 5.0}
    stats.record(orders, settings.rule().size(orders, positions, PRICES, CLASSES), positions, CLASSES, 1_000.0)
    assert stats.report().min_capital is None


def test_empty_stats():
    report = LotStats(LotSettings()).report()
    assert report.orders == 0
    assert report.min_capital is None
    assert report.skipped_share == 0.0

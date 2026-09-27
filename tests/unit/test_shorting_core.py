"""Phase 16.1: the signed book in ``core.types`` and ``execution.orders.classify``."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from stonks.core.types import Fill, Order, Portfolio
from stonks.execution.orders import classify, make_client_id, side_token

D = date(2026, 3, 20)
T = datetime(2026, 3, 20, tzinfo=UTC)


def _order(side="sell", qty=10.0, cid="2026-03-20:s:X:sell", effect=None) -> Order:
    return Order(client_id=cid, ticker="X", side=side, quantity=qty, position_effect=effect)


# ---- Portfolio helpers ---------------------------------------------------------------


def test_exposure_helpers_on_a_long_short_book() -> None:
    p = Portfolio(cash=1_000.0, positions={"A": 10.0, "B": -5.0})
    prices = {"A": 20.0, "B": 40.0}
    assert p.long_value(prices) == 200.0
    assert p.short_value(prices) == 200.0  # a magnitude
    assert p.gross(prices) == 400.0
    assert p.net(prices) == 0.0
    # A short is a liability: cash holds its proceeds, the mark subtracts it.
    assert p.total_value(prices) == 1_000.0


def test_exposure_helpers_skip_unpriced() -> None:
    p = Portfolio(cash=0.0, positions={"A": 10.0, "B": -5.0})
    assert p.gross({"A": 1.0}) == 10.0
    assert p.net({"B": 2.0}) == -10.0


def test_short_then_cover_cash_and_pnl_by_hand() -> None:
    # Short 10 at 100 (fee 1), cover 10 at 90 (fee 1): P&L = 10 * 10 - 2 = 98.
    p = Portfolio(cash=1_000.0)
    p.apply_fill(Fill("a", "X", 10.0, 100.0, 1.0, T, "sell"))
    assert p.positions == {"X": -10.0}
    assert p.cash == 1_999.0
    assert p.total_value({"X": 100.0}) == 999.0
    p.apply_fill(Fill("b", "X", 10.0, 90.0, 1.0, T, "buy"))
    assert p.positions == {}
    assert p.cash == 1_098.0


def test_order_position_effect_defaults_to_none_and_is_validated() -> None:
    assert _order().position_effect is None
    with pytest.raises(ValueError, match="position_effect"):
        _order(effect="sideways")  # type: ignore[arg-type]


# ---- side tokens and client ids -------------------------------------------------------


@pytest.mark.parametrize(
    ("side", "effect", "token"),
    [
        ("buy", None, "buy"),
        ("sell", None, "sell"),
        ("buy", "open", "buy"),
        ("sell", "close", "sell"),
        ("sell", "open", "short"),
        ("buy", "close", "cover"),
    ],
)
def test_side_token(side, effect, token) -> None:
    assert side_token(_order(side=side, effect=effect)) == token


def test_make_client_id_takes_short_and_cover_tokens() -> None:
    assert make_client_id(as_of=D, strategy_id="s", ticker="X", side="short") == (
        "2026-03-20:s:X:short"
    )
    assert make_client_id(as_of=D, strategy_id="s", ticker="X", side="cover").endswith(":cover")


# ---- classify ---------------------------------------------------------------------------


def test_classify_long_sell_within_position_is_a_close_with_the_same_id() -> None:
    [leg] = classify(_order(qty=40.0), held=100.0)
    assert leg.position_effect == "close"
    assert leg.client_id == "2026-03-20:s:X:sell"
    assert leg.quantity == 40.0


def test_classify_buy_from_flat_is_an_open() -> None:
    [leg] = classify(_order(side="buy", cid="2026-03-20:s:X:buy"), held=0.0)
    assert (leg.position_effect, leg.client_id) == ("open", "2026-03-20:s:X:buy")


def test_classify_splits_a_sell_that_crosses_zero() -> None:
    close, open_ = classify(_order(qty=150.0), held=100.0)
    assert (close.side, close.position_effect, close.quantity) == ("sell", "close", 100.0)
    assert close.client_id == "2026-03-20:s:X:sell"
    assert (open_.side, open_.position_effect, open_.quantity) == ("sell", "open", 50.0)
    assert open_.client_id == "2026-03-20:s:X:short"


def test_classify_sell_from_flat_is_a_short() -> None:
    [leg] = classify(_order(qty=5.0), held=0.0)
    assert (leg.position_effect, leg.client_id) == ("open", "2026-03-20:s:X:short")


def test_classify_splits_a_buy_that_crosses_zero() -> None:
    cover, open_ = classify(_order(side="buy", qty=15.0, cid="2026-03-20:s:X:buy"), held=-10.0)
    assert (cover.position_effect, cover.quantity, cover.client_id) == (
        "close",
        10.0,
        "2026-03-20:s:X:cover",
    )
    assert (open_.position_effect, open_.quantity, open_.client_id) == (
        "open",
        5.0,
        "2026-03-20:s:X:buy",
    )


def test_classify_retokens_an_id_without_a_side_suffix() -> None:
    [leg] = classify(_order(side="buy", qty=5.0, cid="rule-x"), held=-10.0)
    assert leg.client_id == "rule-x#cover"


def test_classify_float_dust_does_not_open_a_sliver() -> None:
    [leg] = classify(_order(qty=0.1 + 0.2), held=0.3)
    assert leg.position_effect == "close"


def test_classify_keeps_an_already_classified_single_leg() -> None:
    order = _order(qty=5.0, effect="open", cid="2026-03-20:s:X:short")
    assert classify(order, held=0.0) == [order]

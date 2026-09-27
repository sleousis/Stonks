"""The pre-open gap check of the submit window (roadmap 19.14): an opening
order whose latest pre-open quote moved beyond the price band since the
decision is held. Closes are never held (P28)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from stonks.core.types import Order
from stonks.execution.brokers.base import Quote
from stonks.production.live.gap import gap_limit, preopen_holds
from stonks.production.rules.price_band import PriceBandSettings

AT = datetime(2026, 3, 18, 13, 10, tzinfo=UTC)


def _order(ticker="A.US", side="buy", decision=100.0, effect=None) -> Order:
    return Order(
        client_id=f"c-{ticker}-{side}",
        ticker=ticker,
        side=side,  # type: ignore[arg-type]
        quantity=10.0,
        decision_price=decision,
        position_effect=effect,
    )


def _quote(ticker="A.US", last=100.0, delayed=False) -> Quote:
    return Quote(ticker=ticker, last=last, bid=None, ask=None, as_of=AT, delayed=delayed)


def test_the_limit_is_the_max_gap_else_the_band():
    assert gap_limit(None) is None
    assert gap_limit(PriceBandSettings()) is None  # the band is off
    assert gap_limit(PriceBandSettings(band_pct=0.02)) == 0.02
    assert gap_limit(PriceBandSettings(band_pct=0.02, max_gap_pct=0.05)) == 0.05


def test_an_opening_order_that_gapped_is_held():
    holds = preopen_holds([_order()], {"A.US": _quote(last=110.0)}, 0.05)
    assert list(holds) == ["c-A.US-buy"]
    assert "10.00%" in holds["c-A.US-buy"] and "5.00%" in holds["c-A.US-buy"]


def test_a_small_move_passes():
    assert preopen_holds([_order()], {"A.US": _quote(last=103.0)}, 0.05) == {}


def test_a_delayed_quote_still_counts():
    holds = preopen_holds([_order()], {"A.US": _quote(last=90.0, delayed=True)}, 0.05)
    assert list(holds) == ["c-A.US-buy"]


def test_an_opening_order_with_no_quote_is_held():
    holds = preopen_holds([_order()], {}, 0.05)
    assert "no pre-open quote" in holds["c-A.US-buy"]


@pytest.mark.parametrize("effect", ["close", None])
def test_a_close_is_never_held(effect):
    sell = _order(side="sell", effect=effect)
    assert preopen_holds([sell], {"A.US": _quote(last=50.0)}, 0.05) == {}
    assert preopen_holds([sell], {}, 0.05) == {}


def test_a_short_sale_is_an_opening_order():
    short = _order(side="sell", effect="open")
    assert list(preopen_holds([short], {"A.US": _quote(last=80.0)}, 0.05)) == ["c-A.US-sell"]


def test_without_a_decision_price_the_gap_is_unknown_and_not_held():
    assert preopen_holds([_order(decision=None)], {"A.US": _quote(last=500.0)}, 0.05) == {}


def test_off_without_a_limit():
    assert preopen_holds([_order()], {}, None) == {}

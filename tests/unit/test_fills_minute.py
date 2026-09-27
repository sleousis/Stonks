"""The minute fill model for intraday books (roadmap 21.2.3).

A decision on the close of minute t fills at the open of minute t+1 (P21),
capped by that minute's volume (P20), plus the half spread when a recorded
quote is known. Day orders expire at the session end. The daily
``BarFillModel`` must not change.
"""

from __future__ import annotations

import pytest

from stonks.backtest.fills import (
    BarFillModel,
    BarQuote,
    FillModelSettings,
    MinuteFillModel,
    MinuteFillSettings,
)
from stonks.core.types import Order


def order(side: str = "buy", qty: float = 100.0, **kw) -> Order:
    return Order(client_id="c1", ticker="A.US", side=side, quantity=qty, **kw)  # type: ignore[arg-type]


def quote(**kw) -> BarQuote:
    base = {"open": 10.0, "high": 10.5, "low": 9.5, "volume": 10_000.0, "gap_bars": 0.0}
    base.update(kw)
    return BarQuote(**base)


MODEL = MinuteFillModel()


# ---- the daily model is unchanged ----------------------------------------------------------


def test_daily_model_ignores_the_intraday_quote_fields():
    daily = BarFillModel(FillModelSettings())
    plain = BarQuote(open=10.0, high=10.5, low=9.5, volume=10_000.0)
    rich = BarQuote(
        open=10.0, high=10.5, low=9.5, volume=10_000.0,
        bid=9.9, ask=10.1, gap_bars=50.0, new_session=True,
    )  # fmt: skip
    for o in (order(), order("sell"), order(order_type="limit", limit_price=9.8)):
        assert daily.decide(o, rich) == daily.decide(o, plain)


def test_bar_quote_defaults_keep_old_call_sites_working():
    q = BarQuote(open=1.0)
    assert (q.bid, q.ask, q.gap_bars, q.new_session) == (None, None, None, None)


# ---- next-bar fills -----------------------------------------------------------------------


def test_market_order_fills_at_the_open_without_a_quote():
    d = MODEL.decide(order(), quote())
    assert (d.quantity, d.price, d.carry) == (100.0, 10.0, 0.0)


def test_recorded_quote_adds_the_half_spread_to_a_buy():
    d = MODEL.decide(order(), quote(bid=9.98, ask=10.02))
    assert d.price == pytest.approx(10.02)


def test_recorded_quote_takes_the_half_spread_from_a_sell():
    d = MODEL.decide(order("sell"), quote(bid=9.98, ask=10.02))
    assert d.price == pytest.approx(9.98)


def test_half_spread_can_be_turned_off():
    model = MinuteFillSettings(use_quote_spread=False).build()
    assert model.decide(order(), quote(bid=9.98, ask=10.02)).price == 10.0


@pytest.mark.parametrize(("bid", "ask"), [(10.1, 9.9), (0.0, 10.0), (None, 10.0)])
def test_a_crossed_or_partial_quote_adds_nothing(bid, ask):
    assert MODEL.decide(order(), quote(bid=bid, ask=ask)).price == 10.0


def test_limit_buy_never_pays_more_than_its_limit():
    o = order(order_type="limit", limit_price=10.01)
    d = MODEL.decide(o, quote(bid=9.98, ask=10.02))
    assert d.price == pytest.approx(10.01)


def test_limit_sell_never_receives_less_than_its_limit():
    o = order("sell", order_type="limit", limit_price=9.99)
    d = MODEL.decide(o, quote(bid=9.98, ask=10.02))
    assert d.price == pytest.approx(9.99)


def test_untouched_limit_expires():
    d = MODEL.decide(order(order_type="limit", limit_price=9.0), quote())
    assert d.quantity == 0.0
    assert d.reason == "limit_not_reached"
    assert d.carry == 0.0


def test_sell_stop_fills_from_the_bar_range():
    o = order("sell", order_type="stop", stop_price=9.7)
    assert MODEL.decide(o, quote()).price == pytest.approx(9.7)


# ---- participation cap --------------------------------------------------------------------


def test_participation_cap_takes_a_share_of_the_minute_volume():
    d = MODEL.decide(order(qty=2_000.0), quote(volume=5_000.0))
    assert d.quantity == pytest.approx(500.0)
    assert d.carry == pytest.approx(1_500.0)
    assert d.reason == "participation"


def test_zero_volume_minute_carries_the_whole_order():
    d = MODEL.decide(order(), quote(volume=0.0))
    assert d.quantity == 0.0
    assert d.carry == 100.0
    assert d.reason == "zero_volume"


def test_ioc_never_carries():
    d = MODEL.decide(order(qty=2_000.0, time_in_force="ioc"), quote(volume=5_000.0))
    assert d.quantity == pytest.approx(500.0)
    assert d.carry == 0.0


# ---- day orders and gaps ------------------------------------------------------------------


def test_day_order_expires_when_the_fill_bar_is_in_a_new_session():
    d = MODEL.decide(order(time_in_force="day"), quote(new_session=True))
    assert d.quantity == 0.0
    assert d.carry == 0.0
    assert d.reason == "session_end"


def test_order_without_a_time_in_force_is_a_day_order():
    assert MODEL.decide(order(), quote(new_session=True)).reason == "session_end"


def test_expiry_at_the_session_end_can_be_turned_off():
    model = MinuteFillSettings(expire_at_session_end=False).build()
    assert model.decide(order(), quote(new_session=True)).quantity == 100.0


def test_gap_in_bars_beyond_the_limit_expires():
    model = MinuteFillSettings(max_gap_bars=3).build()
    assert model.decide(order(), quote(gap_bars=3.0)).quantity == 100.0
    d = model.decide(order(), quote(gap_bars=4.0))
    assert (d.quantity, d.reason, d.carry) == (0.0, "gap", 0.0)


def test_day_gap_guard_does_not_apply_to_minutes():
    # the daily guard measures calendar days: a minute model measures bars
    d = MODEL.decide(order(), quote(gap_days=30.0))
    assert d.quantity == 100.0


def test_market_stats_spec_is_declared():
    assert MODEL.market_stats_spec.adv_window >= 1

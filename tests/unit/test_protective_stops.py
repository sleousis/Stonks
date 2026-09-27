"""Protective stops, the pure part (roadmap 19.10): which positions need a
stop, where it sits, when it is resized or cancelled, the client ids and OCA
groups, and exits that share the stop's group."""

from __future__ import annotations

from datetime import date

import pytest

from stonks.core.types import Order
from stonks.production.live.stops import (
    Holding,
    LedgerFill,
    WorkingStop,
    holdings_from_fills,
    oca_group_for,
    plan_stops,
    stop_client_id,
    stop_price_for,
    tag_exits,
)
from stonks.production.rules._stop_settings import ProtectiveStopSettings

AS_OF = date(2026, 3, 17)
ON = ProtectiveStopSettings(enabled=True, atr_multiple=2.0)
OFF = ProtectiveStopSettings()


def holding(ticker="A.US", qty=10.0, entry="e1", price=100.0, sid="s1") -> Holding:
    return Holding(ticker=ticker, quantity=qty, entry_client_id=entry, entry_price=price,
                   strategy_id=sid)  # fmt: skip


def working(cid="e1:stop", ticker="A.US", side="sell", qty=10.0, stop=90.0, entry="e1",
            state="accepted") -> WorkingStop:  # fmt: skip
    return WorkingStop(
        client_id=cid,
        ticker=ticker,
        side=side,
        quantity=qty,
        stop_price=stop,
        strategy_id="s1",
        oca_group=oca_group_for("pf", ticker, entry),
        entry_client_id=entry,
        state=state,
    )


def plan(holdings, stops=(), settings=ON, atr=None, closes=None, taken=frozenset()):
    return plan_stops(
        {h.ticker: h for h in holdings},
        list(stops),
        settings_for=lambda sid: settings if not callable(settings) else settings(sid),
        atr=atr if atr is not None else {"A.US": 5.0, "B.US": 2.0},
        closes=closes if closes is not None else {"A.US": 101.0, "B.US": 50.0},
        portfolio_id="pf",
        as_of=AS_OF,
        tick_id="t1",
        taken=taken,
    )


# ---- price -------------------------------------------------------------------------


def test_the_stop_sits_atr_multiples_from_the_entry():
    assert stop_price_for(holding(), ON, atr=5.0, close=101.0) == pytest.approx(90.0)
    short = holding(qty=-10.0)
    assert stop_price_for(short, ON, atr=5.0, close=99.0) == pytest.approx(110.0)


def test_without_an_atr_the_fallback_share_of_the_entry():
    assert stop_price_for(holding(), ON, atr=None, close=101.0) == pytest.approx(90.0)
    wide = ProtectiveStopSettings(enabled=True, fallback_pct=0.2)
    assert stop_price_for(holding(), wide, atr=float("nan"), close=101.0) == pytest.approx(80.0)


def test_the_stop_never_sits_beyond_the_market():
    # the price already fell below the entry-based stop: protect from the close
    assert stop_price_for(holding(), ON, atr=5.0, close=85.0) == pytest.approx(75.0)
    assert stop_price_for(holding(qty=-10.0), ON, atr=5.0, close=115.0) == pytest.approx(125.0)
    assert stop_price_for(holding(price=5.0), ON, atr=5.0, close=5.0) is None


# ---- ids ---------------------------------------------------------------------------


def test_stop_client_id_is_the_entry_plus_stop_then_numbered():
    assert stop_client_id("e1", set()) == "e1:stop"
    assert stop_client_id("e1", {"e1:stop"}) == "e1:stop:2"
    assert stop_client_id("e1", {"e1:stop", "e1:stop:2"}) == "e1:stop:3"


def test_oca_group_is_stable_per_position_entry():
    g = oca_group_for("pf", "A.US", "e1")
    assert g == oca_group_for("pf", "A.US", "e1") and g.startswith("stk-oca-")
    assert g != oca_group_for("pf", "A.US", "e2")
    assert g != oca_group_for("pf2", "A.US", "e1")
    assert len(g) <= 40


# ---- planning ------------------------------------------------------------------------


def test_a_new_position_gets_a_gtc_stop_attributed_to_its_strategy():
    p = plan([holding()])
    [order] = p.place
    assert (order.client_id, order.side, order.quantity) == ("e1:stop", "sell", 10.0)
    assert (order.order_type, order.time_in_force, order.position_effect) == ("stop", "gtc",
                                                                              "close")  # fmt: skip
    assert order.stop_price == pytest.approx(90.0)
    assert (order.strategy_id, order.portfolio_id, order.tick_id) == ("s1", "pf", "t1")
    assert order.oca_group == oca_group_for("pf", "A.US", "e1")
    assert order.decision_context["trigger"] == "stop"
    assert order.decision_context["entry_client_id"] == "e1"
    assert order.decision_price == 101.0
    assert p.cancel == () and p.keep == ()


def test_a_short_gets_a_buy_stop():
    [order] = plan([holding(qty=-4.0)], closes={"A.US": 99.0}).place
    assert (order.side, order.quantity, order.stop_price) == ("buy", 4.0, pytest.approx(110.0))


def test_stops_off_places_nothing_and_cancels_what_works():
    assert plan([holding()], settings=OFF).place == ()
    p = plan([holding()], [working()], settings=OFF)
    assert [(c.stop.client_id, c.reason) for c in p.cancel] == [("e1:stop", "stops turned off")]


def test_per_strategy_settings():
    both = [holding(), holding(ticker="B.US", entry="e2", sid="s2", price=50.0)]
    p = plan(both, settings=lambda sid: ON if sid == "s2" else OFF)
    assert [o.ticker for o in p.place] == ["B.US"]


def test_a_matching_stop_is_kept():
    p = plan([holding()], [working()])
    assert p.place == () and p.cancel == () and [w.client_id for w in p.keep] == ["e1:stop"]


def test_a_closed_position_cancels_its_stop():
    p = plan([], [working()])
    assert [(c.stop.client_id, c.reason) for c in p.cancel] == [("e1:stop", "position closed")]
    assert p.place == ()


def test_a_smaller_position_resizes_the_stop_at_the_same_price():
    p = plan([holding(qty=6.0)], [working(stop=91.5)], taken={"e1:stop"})
    [c] = p.cancel
    assert (c.stop.client_id, c.reason) == ("e1:stop", "resized")
    [order] = p.place
    assert (order.client_id, order.quantity, order.stop_price) == ("e1:stop:2", 6.0, 91.5)
    assert order.decision_context["replaces"] == "e1:stop"
    assert order.oca_group == oca_group_for("pf", "A.US", "e1")


def test_a_grown_position_gets_a_new_stop_priced_from_the_new_entry():
    grown = holding(qty=15.0, entry="e2", price=104.0)
    p = plan([grown], [working()], taken={"e1:stop"})
    assert [c.reason for c in p.cancel] == ["resized"]
    [order] = p.place
    assert (order.client_id, order.quantity) == ("e2:stop", 15.0)
    assert order.stop_price == pytest.approx(94.0)
    assert order.oca_group == oca_group_for("pf", "A.US", "e2")


def test_a_flipped_position_cancels_the_old_side():
    p = plan([holding(qty=-5.0, entry="e3")], [working()], closes={"A.US": 99.0})
    assert [c.reason for c in p.cancel] == ["resized"]
    assert [o.side for o in p.place] == ["buy"]


def test_duplicate_stops_keep_one():
    p = plan([holding()], [working(), working(cid="e1:stop:2")])
    assert [w.client_id for w in p.keep] == ["e1:stop"]
    assert [(c.stop.client_id, c.reason) for c in p.cancel] == [("e1:stop:2", "duplicate")]


def test_no_room_for_a_stop_is_reported():
    p = plan([holding(price=5.0)], closes={"A.US": 5.0}, atr={"A.US": 5.0})
    assert p.place == () and p.unprotected == (("A.US", "no room for a stop below the price"),)


# ---- holdings --------------------------------------------------------------------------


def fill(cid, side, qty, price, ticker="A.US", sid="s1"):
    return LedgerFill(client_id=cid, strategy_id=sid, ticker=ticker, side=side, quantity=qty,
                      price=price)  # fmt: skip


def test_holdings_from_fills_average_cost_and_latest_entry():
    fills = [fill("e1", "buy", 10, 100.0), fill("e2", "buy", 10, 110.0, sid="s2")]
    [h] = holdings_from_fills(fills, {"A.US": 20.0}).values()
    assert (h.quantity, h.entry_client_id, h.strategy_id) == (20.0, "e2", "s2")
    assert h.entry_price == pytest.approx(105.0)


def test_a_partial_exit_keeps_the_entry_and_the_cost():
    fills = [fill("e1", "buy", 10, 100.0), fill("x1", "sell", 4, 120.0)]
    [h] = holdings_from_fills(fills, {"A.US": 6.0}).values()
    assert (h.quantity, h.entry_client_id, h.entry_price) == (6.0, "e1", pytest.approx(100.0))


def test_a_round_trip_resets_and_a_short_is_tracked():
    fills = [fill("e1", "buy", 10, 100.0), fill("x1", "sell", 10, 90.0),
             fill("e2", "sell", 5, 95.0)]  # fmt: skip
    [h] = holdings_from_fills(fills, {"A.US": -5.0}).values()
    assert (h.quantity, h.entry_client_id, h.entry_price) == (-5.0, "e2", pytest.approx(95.0))


def test_only_positions_the_ledger_explains_are_protected():
    fills = [fill("e1", "buy", 10, 100.0)]
    got = holdings_from_fills(fills, {"A.US": 10.0, "OWNER.US": 50.0, "B.US": 0.0})
    assert list(got) == ["A.US"]
    # the ledger says long but the view says short: not ours to protect
    assert holdings_from_fills(fills, {"A.US": -3.0}) == {}


# ---- exits share the stop's group -------------------------------------------------------


def test_closing_orders_join_the_stop_group_and_opening_ones_do_not():
    stop = working()
    exit_order = Order(client_id="x", ticker="A.US", side="sell", quantity=4.0,
                       position_effect="close")  # fmt: skip
    buy = Order(client_id="b", ticker="A.US", side="buy", quantity=2.0, position_effect="open")
    other = Order(client_id="o", ticker="B.US", side="sell", quantity=1.0, position_effect="close")
    tagged = tag_exits([exit_order, buy, other], [stop])
    assert [o.oca_group for o in tagged] == [stop.oca_group, None, None]

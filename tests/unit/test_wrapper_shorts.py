"""Wrappers keep a short-capable inner strategy short-capable (BE-14).

A regime wrapper must report the inner strategy's ``supports_short``, block
only opening orders when risk is off (a cover still goes through), and
cover shorts as well as sell longs in ``exit_all``."""

from __future__ import annotations

from datetime import date

import pytest

from stonks.core.types import Order, Portfolio
from stonks.strategies.base import BaseStrategy
from stonks.strategies.latent_regime import LatentRegimeFilter
from stonks.strategies.macro_regime import MacroRegimeFilter
from stonks.strategies.regime import RegimeFilter

AS_OF = date(2026, 3, 2)
LS = "stonks.strategies.examples.ls_momentum:LongShortMomentum"
SCRIPTED = f"{__name__}:ScriptedInner"


class ScriptedInner(BaseStrategy):
    """A short-capable inner strategy that always sends the same orders."""

    id = "scripted"
    supports_short = True

    def estimate_return(self, ticker, as_of, lake):
        return 1.0

    def decide(self, my_picks, portfolio, prices, as_of):
        def order(ticker: str, side: str, qty: float) -> Order:
            return Order(client_id=f"s:{ticker}:{side}", ticker=ticker, side=side, quantity=qty)

        return [
            order("COVER.US", "buy", 10.0),  # held -10: a cover
            order("NEW.US", "buy", 5.0),  # flat: a new long
            order("SHORT.US", "sell", 4.0),  # flat: a new short
            order("TRIM.US", "sell", 5.0),  # held 5: a close
        ]


class _Lake:
    """Anything weakly referable stands in for the lake the wrapper saw."""


BOOK = Portfolio(cash=1_000.0, positions={"COVER.US": -10.0, "TRIM.US": 5.0})

WRAPPERS = [RegimeFilter, LatentRegimeFilter]


def _risk_off(wrapper, lake):
    wrapper._remember_lake(lake)
    wrapper.is_risk_off = lambda *args, **kwargs: True
    return wrapper


@pytest.mark.parametrize("cls", [*WRAPPERS, MacroRegimeFilter])
@pytest.mark.parametrize("mode", ["short", "flat"])
def test_a_wrapper_reports_the_inner_supports_short(cls, mode):
    w = cls({"inner_class_path": LS, "inner_params": {"short_mode": mode}})
    assert w.inner.supports_short is (mode == "short")
    assert w.supports_short is (mode == "short")


@pytest.mark.parametrize("cls", WRAPPERS)
def test_risk_off_blocks_opening_orders_and_keeps_closes(cls):
    lake = _Lake()
    w = _risk_off(cls({"inner_class_path": SCRIPTED, "inner_params": {}}), lake)
    orders = w.decide([], BOOK, {}, AS_OF)
    kept = {(o.ticker, o.side) for o in orders}
    assert kept == {("COVER.US", "buy"), ("TRIM.US", "sell")}


@pytest.mark.parametrize("cls", WRAPPERS)
def test_exit_all_covers_shorts_too(cls):
    lake = _Lake()
    params = {"inner_class_path": SCRIPTED, "inner_params": {}, "mode": "exit_all"}
    w = _risk_off(cls(params), lake)
    orders = w.decide([], BOOK, {}, AS_OF)
    assert {(o.ticker, o.side, o.quantity) for o in orders} == {
        ("COVER.US", "buy", 10.0),
        ("TRIM.US", "sell", 5.0),
    }
    assert all(o.position_effect == "close" for o in orders)

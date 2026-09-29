"""BE-14: wrappers keep a short-capable inner strategy's shorts, and on risk
off they drop only opening orders and close shorts as well as longs."""

from __future__ import annotations

from datetime import date

import pytest

from stonks.core.types import Order, Portfolio
from stonks.strategies.examples.ls_momentum import LongShortMomentum
from stonks.strategies.latent_regime import LatentRegimeFilter
from stonks.strategies.macro_regime import MacroRegimeFilter
from stonks.strategies.regime import RegimeFilter

AS_OF = date(2026, 3, 20)
INNER = f"{LongShortMomentum.__module__}:{LongShortMomentum.__name__}"
WRAPPERS = [RegimeFilter, LatentRegimeFilter]


def _wrap(cls, **params):
    return cls({"inner_class_path": INNER, "inner_params": {"short_mode": "short"}, **params})


@pytest.mark.parametrize("cls", [*WRAPPERS, MacroRegimeFilter])
def test_be14_a_wrapper_adopts_supports_short(cls):
    assert _wrap(cls).supports_short is True
    flat = cls({"inner_class_path": INNER, "inner_params": {"short_mode": "flat"}})
    assert flat.supports_short is False


def _risk_off(monkeypatch, wrapper, orders):
    monkeypatch.setattr(wrapper, "_recall_lake", lambda: object())
    monkeypatch.setattr(wrapper, "is_risk_off", lambda *a, **k: True)
    monkeypatch.setattr(wrapper._inner, "decide", lambda *a, **k: list(orders))


@pytest.mark.parametrize("cls", WRAPPERS)
def test_be14_block_mode_keeps_covers_and_drops_opens(cls, monkeypatch):
    wrapper = _wrap(cls, mode="block_new_buys")
    book = Portfolio(cash=1_000.0, positions={"S.US": -10.0, "L.US": 5.0})
    orders = [
        Order("cover", "S.US", "buy", 10.0),  # closes a short: kept
        Order("open", "N.US", "buy", 3.0),  # opens a long: dropped
        Order("short", "M.US", "sell", 4.0),  # opens a short: dropped
        Order("sell", "L.US", "sell", 5.0),  # closes a long: kept
        Order("flip", "S.US", "buy", 0.0 + 12.0),  # covers 10, opens 2 long
    ]
    _risk_off(monkeypatch, wrapper, orders)
    out = wrapper.decide([], book, {}, AS_OF)
    kept = [(o.ticker, o.side, o.quantity) for o in out]
    assert kept == [("S.US", "buy", 10.0), ("L.US", "sell", 5.0)]
    assert all(o.position_effect == "close" for o in out)


@pytest.mark.parametrize("cls", WRAPPERS)
def test_be14_exit_all_covers_shorts_too(cls, monkeypatch):
    wrapper = _wrap(cls, mode="exit_all")
    book = Portfolio(cash=1_000.0, positions={"S.US": -10.0, "L.US": 5.0})
    _risk_off(monkeypatch, wrapper, [])
    out = {(o.ticker, o.side, o.quantity) for o in wrapper.decide([], book, {}, AS_OF)}
    assert out == {("S.US", "buy", 10.0), ("L.US", "sell", 5.0)}


@pytest.mark.parametrize(
    "path", ["trailing_stop:TrailingStopWrapper", "last_trade_filter:LastTradeFilter"]
)
def test_long_only_wrappers_never_short(path):
    """The trailing stop and the last trade filter replay a long trade log
    (a high-water mark less k ATRs, long winners and losers). Adopting a
    short inner's ``supports_short`` would stop a winning short on a fall
    and judge short entries on long outcomes, so they stay long-only."""
    import importlib

    module, name = path.split(":")
    cls = getattr(importlib.import_module(f"stonks.strategies.{module}"), name)
    assert _wrap(cls).inner.supports_short is True
    assert _wrap(cls).supports_short is False

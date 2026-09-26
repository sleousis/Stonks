"""Behaviour shared by the four neurotrader888 chart-pattern strategies:
valid default params, long-only decide(), save/load round-trip and catalog
discovery."""

from __future__ import annotations

import importlib
import json
from datetime import date

import pytest

from stonks.app.catalog import PackageStrategySource
from stonks.core.params import validate_params
from stonks.core.protocols import Strategy
from stonks.core.types import Portfolio
from stonks.strategies.examples.flag_pennant import FlagPennantStrategy
from stonks.strategies.examples.harmonic_xabcd import HarmonicXABCDStrategy
from stonks.strategies.examples.head_shoulders import HeadShouldersStrategy
from stonks.strategies.examples.market_structure_break import MarketStructureBreakStrategy

CLASSES = [
    HeadShouldersStrategy,
    FlagPennantStrategy,
    HarmonicXABCDStrategy,
    MarketStructureBreakStrategy,
]


@pytest.mark.parametrize("cls", CLASSES)
def test_defaults_are_valid_and_protocol_is_satisfied(cls):
    spec = cls.parameter_spec()
    validate_params({s.name: s.default for s in spec}, spec)
    s = cls({"ticker": "X.US"})
    assert isinstance(s, Strategy)
    assert cls.applicable_asset_classes == ("crypto", "equity")
    assert {"ticker", "interval", "allocation"} <= {p.name for p in spec}
    assert all(not p.tunable for p in spec if p.name in {"ticker", "interval", "allocation"})


@pytest.mark.parametrize("cls", CLASSES)
def test_decide_buys_when_picked_and_flat(cls):
    s = cls({"ticker": "X.US", "allocation": 0.5})
    orders = s.decide(
        [(0.02, "X.US")], Portfolio(cash=1_000.0, positions={}), {"X.US": 10.0}, date(2026, 3, 1)
    )
    assert len(orders) == 1
    assert orders[0].side == "buy" and orders[0].quantity == pytest.approx(50.0)
    assert orders[0].strategy_id == cls.id


@pytest.mark.parametrize("cls", CLASSES)
def test_decide_flattens_when_not_picked(cls):
    s = cls({"ticker": "X.US"})
    orders = s.decide(
        [], Portfolio(cash=0.0, positions={"X.US": 7.0}), {"X.US": 10.0}, date(2026, 3, 1)
    )
    assert [(o.side, o.quantity) for o in orders] == [("sell", 7.0)]


@pytest.mark.parametrize("cls", CLASSES)
def test_decide_never_shorts_or_pyramids(cls):
    s = cls({"ticker": "X.US"})
    assert (
        s.decide([], Portfolio(cash=1_000.0, positions={}), {"X.US": 10.0}, date(2026, 3, 1)) == []
    )
    held = Portfolio(cash=1_000.0, positions={"X.US": 3.0})
    assert s.decide([(0.1, "X.US")], held, {"X.US": 10.0}, date(2026, 3, 1)) == []


@pytest.mark.parametrize("cls", CLASSES)
def test_save_load_round_trip_via_class_path(cls, tmp_path):
    s = cls({"ticker": "X.US", "interval": "1h"})
    s.save(tmp_path / "bundle")
    meta = json.loads((tmp_path / "bundle" / "meta.json").read_text())
    module, name = meta["class_path"].split(":")
    loaded_cls = getattr(importlib.import_module(module), name)
    assert loaded_cls is cls
    loaded = loaded_cls.load(tmp_path / "bundle")
    assert loaded.params == s.params
    assert meta["id"] == cls.id


def test_catalog_discovers_all_four():
    found = set(PackageStrategySource("stonks.strategies.examples").discover())
    assert set(CLASSES) <= found

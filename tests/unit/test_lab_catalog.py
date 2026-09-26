"""Strategy catalog for `stonks lab run`."""

from __future__ import annotations

import pytest

from stonks.lab.catalog import resolve_strategy, strategy_catalog
from stonks.strategies.examples.buy_and_hold import BuyAndHold
from stonks.strategies.examples.momentum import Momentum
from stonks.strategies.examples.quality_value import QualityValue
from stonks.strategies.macro_regime import MacroRegimeFilter


def test_catalog_lists_the_examples_and_the_macro_filter_by_id():
    catalog = strategy_catalog()
    assert catalog["buy_and_hold"] is BuyAndHold
    assert catalog["momentum"] is Momentum
    assert catalog["quality_value"] is QualityValue
    assert catalog["macro_regime_filter"] is MacroRegimeFilter
    # only concrete strategies defined in the example modules, no helpers
    assert all(hasattr(cls, "parameter_spec") for cls in catalog.values())


@pytest.mark.parametrize(
    "name",
    ["momentum", "Momentum", "stonks.strategies.examples.momentum:Momentum"],
)
def test_resolve_accepts_id_class_name_or_class_path(name):
    assert resolve_strategy(name) is Momentum


def test_resolve_unknown_lists_the_choices():
    with pytest.raises(ValueError, match="momentum"):
        resolve_strategy("nope")

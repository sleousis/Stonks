"""BL-26: strategy metadata (hypothesis card, capability hooks) and the
``asset_classes`` instance override."""

from __future__ import annotations

import json
from typing import get_args

import pytest

from stonks.core.params import ParameterSpec
from stonks.core.types import AssetClass
from stonks.lab.catalog import strategy_catalog
from stonks.strategies.base import (
    ALPHA_FAMILIES,
    PREMISES,
    BaseStrategy,
    StrategyMetadata,
    strategy_metadata,
)
from stonks.strategies.examples.buy_and_hold import BuyAndHold
from stonks.strategies.macro_regime import MacroRegimeFilter
from stonks.strategies.rule_based import RuleStrategy


class _Plain(BaseStrategy):
    id = "plain"

    @classmethod
    def parameter_spec(cls):
        return [ParameterSpec(name="x", kind="int", default=1, bounds=(0, 10))]


class _Carded(BaseStrategy):
    id = "carded"
    hypothesis = "Slow diffusion of news: winners keep winning for 3-12 months."
    alpha_family = "trend"
    premise = "trend"
    label_horizon_bars = 21
    required_history_bars = 252


def test_base_defaults():
    meta = strategy_metadata(_Plain)
    assert meta == StrategyMetadata(
        hypothesis="",
        alpha_family="other",
        premise="none",
        label_horizon_bars=0,
        required_history_bars=0,
        applicable_asset_classes=("equity",),
    )


def test_declared_card_is_read_from_class_and_instance():
    for target in (_Carded, _Carded({})):
        meta = strategy_metadata(target)
        assert meta.alpha_family == "trend"
        assert meta.premise == "trend"
        assert meta.label_horizon_bars == 21
        assert meta.required_history_bars == 252
        assert meta.hypothesis.startswith("Slow diffusion")


def test_metadata_of_a_non_base_strategy_falls_back_to_defaults():
    class Duck:
        id = "duck"

    meta = strategy_metadata(Duck)
    assert meta.alpha_family == "other"
    assert meta.applicable_asset_classes == ("equity",)


@pytest.mark.parametrize("cls", list(strategy_catalog().values()), ids=lambda c: c.id)
def test_every_catalogued_strategy_has_valid_metadata(cls):
    meta = strategy_metadata(cls)
    assert isinstance(meta.hypothesis, str)
    assert meta.alpha_family in ALPHA_FAMILIES
    assert meta.premise in PREMISES
    assert meta.label_horizon_bars >= 0
    assert meta.required_history_bars >= 0
    assert meta.applicable_asset_classes


@pytest.mark.parametrize(
    ("attr", "value"),
    [
        ("alpha_family", "astrology"),
        ("premise", "vibes"),
        ("label_horizon_bars", -1),
        ("required_history_bars", 2.5),
        ("hypothesis", None),
    ],
)
def test_invalid_class_metadata_is_rejected_at_definition(attr, value):
    with pytest.raises(ValueError, match=attr):
        type("Bad", (BaseStrategy,), {"id": "bad", attr: value})


def test_asset_classes_override_replaces_class_default():
    s = _Plain({"asset_classes": ["crypto", "equity"]})
    assert s.applicable_asset_classes == ("crypto", "equity")
    assert s.params["asset_classes"] == ["crypto", "equity"]
    assert strategy_metadata(s).applicable_asset_classes == ("crypto", "equity")
    # the class itself is untouched
    assert _Plain.applicable_asset_classes == ("equity",)
    assert _Plain({}).applicable_asset_classes == ("equity",)


@pytest.mark.parametrize("bad", [[], (), "crypto", ["crypto", "gold"], None])
def test_bad_asset_classes_override_is_rejected(bad):
    with pytest.raises(ValueError, match="asset_classes"):
        _Plain({"asset_classes": bad})


def test_override_survives_save_and_load(tmp_path):
    s = BuyAndHold({"ticker": "BTC-USD.CC", "asset_classes": ["crypto"]})
    s.save(tmp_path)
    loaded = BuyAndHold.load(tmp_path)
    assert loaded.applicable_asset_classes == ("crypto",)


def test_override_wins_over_classes_derived_at_init():
    """RuleStrategy derives its classes from the spec universe; an explicit
    override still wins (it is the opt-in, out-of-evidence-base switch)."""
    s = RuleStrategy({"asset_classes": ["bond"]})
    assert s.applicable_asset_classes == ("bond",)
    assert RuleStrategy({}).applicable_asset_classes != ("bond",)


def test_wrapper_inherits_the_inner_override():
    wrapped = MacroRegimeFilter(
        {
            "inner_class_path": "stonks.strategies.examples.buy_and_hold:BuyAndHold",
            "inner_params": {"ticker": "X.CC", "asset_classes": ["crypto"]},
        }
    )
    assert wrapped.applicable_asset_classes == ("crypto",)


def test_meta_json_carries_metadata(tmp_path):
    _Carded({}).save(tmp_path)
    meta = json.loads((tmp_path / "meta.json").read_text())
    assert meta["metadata"]["alpha_family"] == "trend"
    assert meta["metadata"]["label_horizon_bars"] == 21
    assert meta["metadata"]["applicable_asset_classes"] == ["equity"]


def test_literal_sets_match_spec():
    assert set(ALPHA_FAMILIES) == {
        "trend",
        "reversion",
        "carry",
        "value",
        "quality",
        "growth",
        "sentiment",
        "data_driven",
        "benchmark",
        "other",
    }
    assert set(PREMISES) == {"trend", "mean_reversion", "none"}
    assert set(get_args(AssetClass)) >= {"equity", "crypto"}

"""Roadmap 22.6: which strategies learn from data, so scheduled retraining applies."""

from __future__ import annotations

from stonks.lifecycle.retrain import is_retrainable
from stonks.strategies.examples.buy_and_hold import BuyAndHold
from stonks.strategies.examples.trendline_meta_label import TrendlineMetaLabelStrategy
from stonks.strategies.trailing_stop import TrailingStopWrapper

_BH = "stonks.strategies.examples.buy_and_hold:BuyAndHold"
_ML = "stonks.strategies.examples.trendline_meta_label:TrendlineMetaLabelStrategy"


def test_rule_strategy_is_not_retrainable():
    assert BuyAndHold({"ticker": "A.US"}).retrainable is False
    assert is_retrainable(BuyAndHold({"ticker": "A.US"})) is False


def test_strategy_that_fits_is_retrainable():
    assert TrendlineMetaLabelStrategy({"ticker": "A.US"}).retrainable is True


def test_wrapper_follows_its_inner_strategy():
    plain = TrailingStopWrapper({"inner_class_path": _BH, "inner_params": {"ticker": "A.US"}})
    ml = TrailingStopWrapper({"inner_class_path": _ML, "inner_params": {"ticker": "A.US"}})
    assert plain.retrainable is False
    assert ml.retrainable is True


def test_object_without_the_flag_is_not_retrainable():
    assert is_retrainable(object()) is False

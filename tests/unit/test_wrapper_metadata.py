"""RS-02 and RS-01: every wrapper reports its inner strategy's label horizon,
required history and data tickers, so the embargo, the preflight and the lab
snapshots see what the wrapped strategy needs."""

from __future__ import annotations

import pytest

from stonks.lab.dataset import LabDataset
from stonks.strategies.base import strategy_data_tickers, strategy_metadata
from stonks.strategies.examples.quant_momentum import QuantMomentum
from stonks.strategies.feature_regime import FeatureRegimeFilter
from stonks.strategies.last_trade_filter import LastTradeFilter
from stonks.strategies.macro_regime import MacroRegimeFilter
from stonks.strategies.regime import RegimeFilter
from stonks.strategies.trailing_stop import TrailingStopWrapper

QM = "stonks.strategies.examples.quant_momentum:QuantMomentum"
IMD = "stonks.strategies.examples.intramarket_difference:IntramarketDifferenceStrategy"
WRAPPERS = [
    RegimeFilter,
    TrailingStopWrapper,
    LastTradeFilter,
    FeatureRegimeFilter,
    MacroRegimeFilter,
]


@pytest.mark.parametrize("cls", WRAPPERS, ids=lambda c: c.__name__)
def test_wrapper_forwards_label_horizon_and_required_history(cls):
    inner = QuantMomentum({})
    wrapped = cls({"inner_class_path": QM, "inner_params": {}})
    assert wrapped.label_horizon_bars == inner.label_horizon_bars > 0
    assert wrapped.required_history_bars >= inner.required_history_bars > 0
    meta = strategy_metadata(wrapped)
    assert meta.label_horizon_bars == inner.label_horizon_bars


@pytest.mark.parametrize("cls", WRAPPERS, ids=lambda c: c.__name__)
def test_wrapper_embargo_follows_the_inner_label_horizon(cls, tmp_path):
    wrapped = cls({"inner_class_path": QM, "inner_params": {}})
    ds = LabDataset(lake=None, universe=["A.US"])  # type: ignore[arg-type]
    assert ds.for_strategy(wrapped).embargo_bars == QuantMomentum({}).label_horizon_bars


@pytest.mark.parametrize("cls", WRAPPERS, ids=lambda c: c.__name__)
def test_wrapper_forwards_inner_data_tickers(cls):
    wrapped = cls({"inner_class_path": IMD, "inner_params": {"reference_ticker": "REF.CC"}})
    assert "REF.CC" in strategy_data_tickers(wrapped)


@pytest.mark.parametrize("cls", WRAPPERS, ids=lambda c: c.__name__)
def test_wrapper_metadata_survives_save_and_load(cls, tmp_path):
    wrapped = cls({"inner_class_path": QM, "inner_params": {}})
    wrapped.save(tmp_path)
    loaded = cls.load(tmp_path)
    assert loaded.label_horizon_bars == wrapped.label_horizon_bars
    assert loaded.required_history_bars == wrapped.required_history_bars


def test_macro_regime_filter_reuses_the_shared_wrapper():
    from stonks.strategies._wrapping import InnerStrategyWrapper

    assert issubclass(MacroRegimeFilter, InnerStrategyWrapper)

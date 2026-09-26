"""RS-07 / RS-30: ``label_horizon_bars`` and ``required_history_bars`` follow
the params, so the embargo covers the horizon the tuner picked and the
preflight's history checks match what the strategy reads."""

from __future__ import annotations

import pytest

from stonks.lab.catalog import is_wrapper, strategy_catalog
from stonks.lab.dataset import LabDataset
from stonks.strategies.base import strategy_metadata
from stonks.strategies.examples._nt888_base import SingleTickerLongFlat
from stonks.strategies.examples.donchian_breakout import DonchianBreakout
from stonks.strategies.examples.ma_crossover import MACrossoverStrategy
from stonks.strategies.examples.momentum import Momentum
from stonks.strategies.examples.pip_miner import PIPMinerStrategy
from stonks.strategies.examples.quant_momentum import QuantMomentum
from stonks.strategies.examples.rsi_pca import RSIPCAStrategy
from stonks.strategies.examples.stocks_on_the_move import StocksOnTheMove
from stonks.strategies.examples.trendline_meta_label import TrendlineMetaLabelStrategy
from stonks.strategies.examples.vsa import VSAStrategy

HORIZONS = [
    (RSIPCAStrategy, {"lookahead": 48}, 48),
    (RSIPCAStrategy, {"hold_bars": 40}, 40),
    (PIPMinerStrategy, {"hold": 24}, 24),
    (TrendlineMetaLabelStrategy, {"hold_period": 48}, 48),
    (VSAStrategy, {"hold_bars": 48}, 48),
]


@pytest.mark.parametrize(("cls", "params", "at_least"), HORIZONS)
def test_label_horizon_follows_params(cls, params, at_least):
    strategy = cls(params)
    assert strategy.label_horizon_bars >= at_least
    assert strategy_metadata(strategy).label_horizon_bars >= at_least


@pytest.mark.parametrize(
    "cls",
    [c for c in strategy_catalog().values() if not is_wrapper(c)],
    ids=lambda c: c.__name__,
)
def test_class_values_are_the_default_params_values(cls):
    """The catalog and the API show the class values: they must match an
    instance built from the default params."""
    strategy = cls({})
    assert strategy.label_horizon_bars == cls.label_horizon_bars
    assert strategy.required_history_bars == cls.required_history_bars


def test_embargo_follows_the_tuned_horizon():
    ds = LabDataset(lake=None, universe=["A.US"])  # type: ignore[arg-type]
    assert ds.for_strategy(RSIPCAStrategy({"lookahead": 48})).embargo_bars >= 48


HISTORY = [
    (QuantMomentum, {"formation_bars": 504}, 505),
    (StocksOnTheMove, {"lookback": 250, "ma_filter": 250, "index_ma": 250}, 251),
    (Momentum, {"lookback_days": 252, "skip_days": 21}, 252 + 21 + 1),
    (DonchianBreakout, {"lookback": 252}, 252),
    (MACrossoverStrategy, {"fast": 20, "slow": 200}, 200),
    (RSIPCAStrategy, {"rsi_period_max": 60}, 60),
]


@pytest.mark.parametrize(("cls", "params", "at_least"), HISTORY)
def test_required_history_follows_params(cls, params, at_least):
    assert cls(params).required_history_bars >= at_least


@pytest.mark.parametrize(
    "cls",
    [c for c in strategy_catalog().values() if issubclass(c, SingleTickerLongFlat)],
    ids=lambda c: c.__name__,
)
def test_every_nt888_port_declares_its_history(cls):
    assert cls({}).required_history_bars > 0


def test_class_metadata_stays_an_int():
    # the catalog and the API read class-level metadata
    for cls in strategy_catalog().values():
        assert isinstance(cls.label_horizon_bars, int)
        assert isinstance(cls.required_history_bars, int)

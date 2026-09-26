"""[backtest] / [lab] settings: cost model and walk-forward defaults."""

from __future__ import annotations

from pathlib import Path

import pytest

from stonks.backtest.costs import AssetClassCostModel, CostModelSettings, Trade
from stonks.config import load_settings
from stonks.lab.parallel import ParallelSettings
from stonks.lab.survival.walk_forward import WalkForwardConfig

REPO_DEFAULT_TOML = Path(__file__).resolve().parents[2] / "config" / "default.toml"


def test_costs_default_to_realistic():
    """BL-13: loading the default settings gives non-zero costs."""
    settings = load_settings(config_path=Path("does-not-exist.toml"))
    assert settings.backtest.costs == CostModelSettings.realistic()
    model = settings.backtest.costs.build()
    assert isinstance(model, AssetClassCostModel)
    cost = model.cost(Trade(ticker="X.US", side="buy", quantity=10.0, price=100.0))
    assert cost.fill_price > 100.0 and cost.fee > 0.0


def test_shipped_default_toml_has_realistic_costs_and_default_lab_settings():
    settings = load_settings(config_path=REPO_DEFAULT_TOML)
    assert settings.backtest.costs == CostModelSettings.realistic()
    assert settings.lab.walk_forward == WalkForwardConfig()
    assert settings.lab.parallel == ParallelSettings(max_workers=0, blas_threads=1)
    assert settings.production.dividend_withholding_rate == 0.0


def test_zero_costs_can_be_configured_explicitly(tmp_path):
    cfg = tmp_path / "cfg.toml"
    cfg.write_text("[backtest.costs]\nimpact_bps = 0.0\n")
    assert load_settings(config_path=cfg).backtest.costs == CostModelSettings()


def test_parallel_settings_read_from_toml(tmp_path):
    cfg = tmp_path / "cfg.toml"
    cfg.write_text("[lab.parallel]\nmax_workers = 3\nblas_threads = 2\n")
    parallel = load_settings(config_path=cfg).lab.parallel
    assert (parallel.max_workers, parallel.blas_threads) == (3, 2)
    assert parallel.resolved_workers() == 3


def test_costs_read_from_toml(tmp_path):
    cfg = tmp_path / "cfg.toml"
    cfg.write_text(
        """
[backtest.costs]
impact_bps = 50.0

[backtest.costs.default]
half_spread_bps = 2.0

[backtest.costs.asset_classes.crypto]
fee_bps = 10.0
""".strip()
    )
    costs = load_settings(config_path=cfg).backtest.costs
    assert costs.impact_bps == 50.0
    assert costs.default.half_spread_bps == 2.0
    assert costs.for_asset_class("crypto").fee_bps == 10.0
    assert costs.for_asset_class("equity").half_spread_bps == 2.0


def test_walk_forward_defaults_read_from_toml(tmp_path):
    cfg = tmp_path / "cfg.toml"
    cfg.write_text("[lab.walk_forward]\nn_splits = 6\ntest_days = 21\nanchored = true\n")
    wf = load_settings(config_path=cfg).lab.walk_forward
    assert (wf.n_splits, wf.test_days, wf.anchored) == (6, 21, True)


def test_unknown_walk_forward_key_is_rejected(tmp_path):
    cfg = tmp_path / "cfg.toml"
    cfg.write_text("[lab.walk_forward]\nsplits = 6\n")
    with pytest.raises(ValueError, match="splits"):
        load_settings(config_path=cfg)

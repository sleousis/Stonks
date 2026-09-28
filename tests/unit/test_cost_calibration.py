"""Cost model calibration for minute trading (roadmap 21.3.5)."""

from __future__ import annotations

import math
import tomllib
from datetime import date

import numpy as np
import pytest

from stonks.backtest.cost_calibration import FillSample, costs_toml, fit_minute_costs
from stonks.backtest.costs import AssetClassCosts, CostModelSettings


def _samples(half_spread: float, impact: float, n: int = 60, seed: int = 7) -> list[FillSample]:
    rng = np.random.default_rng(seed)
    out = []
    for _ in range(n):
        volume = float(rng.integers(1_000, 20_000))
        qty = float(rng.integers(10, 2_000))
        cost = half_spread + impact * math.sqrt(qty / volume)
        out.append(
            FillSample(
                asset_class="equity",
                quantity=qty,
                bar_volume=volume,
                cost_bps=cost,
                half_spread_bps=half_spread,
                notional=qty * 100.0,
                fee=0.0,
            )
        )
    return out


def test_fit_recovers_the_half_spread_and_impact():
    quotes = {"equity": [3.0] * 50}
    fit = fit_minute_costs(
        quotes, _samples(3.0, 40.0), CostModelSettings.realistic(), end=date(2026, 9, 1)
    )
    assert fit.half_spreads["equity"] == pytest.approx(3.0)
    assert fit.impact_bps == pytest.approx(40.0, rel=1e-6)
    assert fit.impact_r2 == pytest.approx(1.0)
    assert fit.proposed.for_asset_class("equity").half_spread_bps == pytest.approx(3.0)
    assert fit.proposed.impact_bps == pytest.approx(40.0, rel=1e-6)
    assert fit.proposed.impact_model == "sqrt"
    # fees and other classes stay as they were
    assert fit.proposed.for_asset_class("equity").fee_bps == 0.5
    assert fit.proposed.for_asset_class("crypto").half_spread_bps == 5.0


def test_half_spread_is_the_median_of_the_quotes():
    quotes = {"equity": [1.0, 2.0, 2.0, 50.0] * 5}
    fit = fit_minute_costs(quotes, [], CostModelSettings.realistic(), end=date(2026, 9, 1))
    assert fit.half_spreads["equity"] == pytest.approx(2.0)


def test_too_little_data_keeps_the_current_values():
    current = CostModelSettings.realistic()
    fit = fit_minute_costs(
        {"equity": [9.0] * 3}, _samples(3.0, 40.0, n=4), current, end=date(2026, 9, 1)
    )
    assert fit.impact_bps is None
    assert fit.proposed.impact_bps == current.impact_bps
    assert fit.proposed.for_asset_class("equity").half_spread_bps == 2.0
    assert any("impact" in note for note in fit.notes)
    assert any("half spread" in note for note in fit.notes)


def test_negative_impact_is_clipped_to_zero():
    samples = _samples(3.0, 0.0)
    samples = [
        FillSample(
            s.asset_class,
            s.quantity,
            s.bar_volume,
            s.cost_bps - 5.0,
            s.half_spread_bps,
            s.notional,
            s.fee,
        )
        for s in samples
    ]
    fit = fit_minute_costs(
        {"equity": [3.0] * 50}, samples, CostModelSettings.realistic(), end=date(2026, 9, 1)
    )
    assert fit.impact_bps == 0.0


def test_missing_fill_quotes_fall_back_to_the_fitted_half_spread():
    samples = [
        FillSample(s.asset_class, s.quantity, s.bar_volume, s.cost_bps, None, s.notional, s.fee)
        for s in _samples(4.0, 25.0)
    ]
    fit = fit_minute_costs(
        {"equity": [4.0] * 20}, samples, CostModelSettings.realistic(), end=date(2026, 9, 1)
    )
    assert fit.impact_bps == pytest.approx(25.0, rel=1e-6)


def test_a_class_without_its_own_entry_starts_from_the_default():
    current = CostModelSettings(default=AssetClassCosts(half_spread_bps=1.0, fee_bps=2.0))
    fit = fit_minute_costs({"crypto": [6.0] * 20}, [], current, end=date(2026, 9, 1))
    crypto = fit.proposed.for_asset_class("crypto")
    assert crypto.half_spread_bps == pytest.approx(6.0)
    assert crypto.fee_bps == 2.0


def test_the_proposed_block_is_valid_toml_for_backtest_costs():
    fit = fit_minute_costs(
        {"equity": [3.0] * 50},
        _samples(3.0, 40.0),
        CostModelSettings.realistic(),
        end=date(2026, 9, 1),
    )
    text = fit.to_toml()
    assert "never applied" in text.lower()
    parsed = tomllib.loads(text)
    costs = CostModelSettings.model_validate(parsed["backtest"]["costs"])
    assert costs == fit.proposed


def test_costs_toml_round_trips_the_realistic_settings():
    settings = CostModelSettings.realistic()
    parsed = tomllib.loads(costs_toml(settings))
    assert CostModelSettings.model_validate(parsed["backtest"]["costs"]) == settings

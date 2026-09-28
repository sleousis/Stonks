"""Roadmap 8.2: the simulated tick fills through the configured cost model.

Backtests fill at the next bar's open through ``[backtest.costs]``; the tick
fills at the latest close. Both hand the same ``CostModel`` the same inputs
(reference price, quantity, asset class, that bar's volume), so a trade at
a given reference price costs the same in both worlds.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from stonks.backtest.costs import AssetClassCosts, CostModelSettings, Trade
from stonks.config import Settings
from stonks.core.protocols import SurvivalReport
from stonks.ingest.pipeline import _rows_to_df
from stonks.ingest.schemas import TickerProfile
from stonks.production.settings_builder import build_tick_settings
from stonks.production.tick import TickSettings, run_tick
from stonks.registry.store import StrategyRegistry
from stonks.store.state import SqliteState
from stonks.strategies.examples.buy_and_hold import BuyAndHold
from tests.fixtures.governance import seed_status

AS_OF = date(2026, 3, 20)
MODEL = CostModelSettings(
    default=AssetClassCosts(half_spread_bps=2.0, fee_bps=1.0),
    asset_classes={"crypto": AssetClassCosts(half_spread_bps=10.0, fee_bps=20.0, fee_flat=0.5)},
    impact_bps=100.0,
)


class CryptoBuyAndHold(BuyAndHold):
    id = "crypto_buy_and_hold"
    applicable_asset_classes = ("crypto",)


def _close(ticker_up_on: date) -> float:
    days = list(pd.bdate_range(start="2025-10-01", end="2026-04-01").date)
    return 100.0 + 100.0 * days.index(ticker_up_on) / (len(days) - 1)


def _env(tmp_path, lake, strategy):
    state = SqliteState(tmp_path / "state.sqlite")
    state.migrate()
    registry = StrategyRegistry(state=state, artifacts_dir=tmp_path / "artifacts")
    sid = registry.register(
        strategy, reports=[SurvivalReport(test_id="oos", passed=True, metrics={})]
    )
    seed_status(registry, sid, "active")
    return state, registry


def _fill(state):
    [row] = state.sql("SELECT quantity, price, fee FROM fills")
    return row


def _expected(qty: float, asset_class: str = "equity"):
    return MODEL.build().cost(
        Trade(
            ticker="UP.US",
            side="buy",
            quantity=qty,
            price=_close(AS_OF),
            asset_class=asset_class,
            bar_volume=1_000_000.0,
        )
    )


def test_tick_fills_through_the_configured_cost_model(tmp_path, lake_trending):
    state, registry = _env(
        tmp_path, lake_trending, BuyAndHold({"ticker": "UP.US", "allocation": 0.5})
    )
    settings = Settings(
        production={"slippage_bps": 50.0, "fee_per_trade": 9.0, "paper_fills": "close"},
        backtest={"costs": MODEL},
    )
    run_tick(state, lake_trending, registry, build_tick_settings(settings, ["UP.US"]), as_of=AS_OF)

    fill = _fill(state)
    expected = _expected(fill["quantity"])
    # the model only: no legacy slippage or flat fee on top
    assert fill["price"] == pytest.approx(expected.fill_price)
    assert fill["fee"] == pytest.approx(expected.fee)
    state.close()


def test_tick_passes_asset_classes_to_the_cost_model(tmp_path, lake_trending):
    lake_trending.upsert_instrument_profile(
        _rows_to_df([TickerProfile(id="UP.US", asset_class="crypto")])
    )
    state, registry = _env(
        tmp_path, lake_trending, CryptoBuyAndHold({"ticker": "UP.US", "allocation": 0.5})
    )
    run_tick(
        state, lake_trending, registry, TickSettings(universe=["UP.US"], costs=MODEL), as_of=AS_OF
    )

    fill = _fill(state)
    expected = _expected(fill["quantity"], asset_class="crypto")
    assert fill["price"] == pytest.approx(expected.fill_price)
    assert fill["fee"] == pytest.approx(expected.fee)
    state.close()


def test_legacy_production_costs_apply_when_backtest_costs_are_unset(tmp_path, lake_trending):
    state, registry = _env(
        tmp_path, lake_trending, BuyAndHold({"ticker": "UP.US", "allocation": 0.5})
    )
    settings = Settings(
        production={"slippage_bps": 50.0, "fee_per_trade": 9.0, "paper_fills": "close"}
    )
    run_tick(state, lake_trending, registry, build_tick_settings(settings, ["UP.US"]), as_of=AS_OF)

    fill = _fill(state)
    assert fill["price"] == pytest.approx(_close(AS_OF) * 1.005)
    assert fill["fee"] == pytest.approx(9.0)
    state.close()


def test_the_configured_paper_book_fills_at_the_next_open(tmp_path, lake_trending):
    """P21: by default a paper order decided at a close stays working and
    the next tick fills it at the next session's open."""
    state, registry = _env(
        tmp_path, lake_trending, BuyAndHold({"ticker": "UP.US", "allocation": 0.5})
    )
    settings = build_tick_settings(Settings(backtest={"costs": MODEL}), ["UP.US"])
    assert settings.paper_fills == "next_open"
    run_tick(state, lake_trending, registry, settings, as_of=AS_OF)
    assert state.sql("SELECT COUNT(*) AS n FROM fills")[0]["n"] == 0
    [order] = state.sql("SELECT status, state FROM orders")
    assert (order["status"], order["state"]) == ("pending", "accepted")

    monday = date(2026, 3, 23)
    run_tick(state, lake_trending, registry, settings, as_of=monday)
    [fill] = state.sql("SELECT price, filled_at, arrival_price FROM fills")
    assert fill["filled_at"].startswith("2026-03-23")
    assert fill["arrival_price"] == pytest.approx(_close(monday))  # the open (= close here)
    assert fill["price"] > _close(monday)  # the buy paid the modelled costs on top
    state.close()


def test_shadow_fills_through_the_same_cost_model(tmp_path, lake_trending):
    state, registry = _env(tmp_path, lake_trending, BuyAndHold({"ticker": "FLAT.US"}))
    shadow = registry.register(
        BuyAndHold({"ticker": "UP.US", "allocation": 0.5}),
        reports=[SurvivalReport(test_id="oos", passed=True, metrics={})],
    )
    seed_status(registry, shadow, "shadow")
    run_tick(
        state,
        lake_trending,
        registry,
        TickSettings(universe=["UP.US", "FLAT.US"], costs=MODEL),
        as_of=AS_OF,
    )

    [decision] = state.sql("SELECT quantity, price FROM shadow_decisions")
    expected = _expected(decision["quantity"])
    assert decision["price"] == pytest.approx(expected.fill_price)
    state.close()

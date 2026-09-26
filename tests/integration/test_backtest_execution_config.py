"""W3.3 wiring: ``[backtest.execution]`` (fill model, settlement) and
``[backtest.construction]`` reach every lab and API backtest."""

from __future__ import annotations

from stonks.backtest.fills import BarFillModel, ExecutionSettings, ImmediateFillModel
from stonks.backtest.simulated_broker import SimulatedBroker
from stonks.config import Settings
from stonks.lab.backtesting import backtest_config, lab_broker
from stonks.lab.dataset import LabDataset


def test_defaults_keep_the_immediate_fill_and_no_construction():
    b = Settings().backtest
    assert b.execution == ExecutionSettings()
    assert b.construction is None


def test_execution_and_construction_parse_from_config():
    b = Settings(
        backtest={
            "execution": {"fill": {"max_participation": 0.05}, "settlement_days": 1},
            "construction": {"method": "equal_weight_top_n"},
        }
    ).backtest
    assert b.execution.fill is not None and b.execution.fill.max_participation == 0.05
    assert b.execution.settlement_days == 1
    assert b.construction is not None and b.construction.method == "equal_weight_top_n"


def test_lab_broker_uses_the_dataset_execution(lake_trending):
    plain = lab_broker(LabDataset(lake=lake_trending, universe=["UP.US"]))
    assert isinstance(plain, SimulatedBroker)
    assert isinstance(plain._fill_model, ImmediateFillModel)
    realistic = lab_broker(
        LabDataset(
            lake=lake_trending,
            universe=["UP.US"],
            execution=ExecutionSettings(fill={"max_participation": 0.1}, settlement_days=1),
        )
    )
    assert isinstance(realistic._fill_model, BarFillModel)
    assert realistic._settlement_days == 1


def test_api_backtests_use_the_configured_execution_and_construction(lake_trending, monkeypatch):
    from datetime import date

    from stonks.app import lab as app_lab
    from stonks.app.lab import BacktestRequest
    from stonks.strategies.examples.buy_and_hold import BuyAndHold

    seen = {}

    class Spy(app_lab.Backtester):
        def __init__(self, *, strategies, broker, lake, config):
            seen["broker"], seen["config"] = broker, config
            super().__init__(strategies=strategies, broker=broker, lake=lake, config=config)

    monkeypatch.setattr(app_lab, "Backtester", Spy)
    settings = Settings(
        backtest={
            "execution": {"fill": {"max_participation": 0.5}, "settlement_days": 2},
            "construction": {"method": "single_winner"},
        }
    )
    request = BacktestRequest(
        strategy={"strategy_id": "x"},
        universe=["UP.US"],
        start=date(2026, 1, 5),
        end=date(2026, 2, 27),
        benchmark="none",
    )
    app_lab.backtest_report(settings, BuyAndHold({"ticker": "UP.US"}), request, lake_trending)
    assert isinstance(seen["broker"]._fill_model, BarFillModel)
    assert seen["broker"]._settlement_days == 2
    assert seen["config"].construction_settings.method == "single_winner"


def test_lab_backtests_take_the_dataset_construction(lake_trending):
    from datetime import date

    from stonks.portfolio.settings import ConstructionSettings

    ds = LabDataset(
        lake=lake_trending,
        universe=["UP.US"],
        construction=ConstructionSettings(method="inverse_vol"),
    )
    config = backtest_config(ds, (date(2026, 1, 1), date(2026, 2, 1)))
    assert config.construction_settings is not None
    assert config.construction_settings.method == "inverse_vol"
    assert (
        backtest_config(
            LabDataset(lake=lake_trending, universe=["UP.US"]), (date(2026, 1, 1), date(2026, 2, 1))
        ).construction
        is None
    )

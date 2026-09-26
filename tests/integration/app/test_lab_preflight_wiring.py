"""The lab-run code path wires the BL-37 preflight: ``[lab] preflight`` and
``strict_preflight`` (overridable per request), issues on the result view,
and a strict failure as a validation error."""

from __future__ import annotations

from datetime import date

import pytest

from stonks.app.errors import ValidationError
from stonks.app.lab import LabRunRequest, execute_lab_run
from stonks.app.strategies import StrategyRef
from stonks.config import Settings
from stonks.strategies.examples.buy_and_hold import BuyAndHold

BAH = "stonks.strategies.examples.buy_and_hold:BuyAndHold"


def _request(**overrides) -> LabRunRequest:
    body = {
        "strategy": StrategyRef(class_path=BAH),
        "universe": ["UP.US", "NOPE.US"],
        "start": date(2025, 10, 1),
        "end": date(2026, 4, 1),
        "budget": 1,
        "grid_size": 1,
        "survival_tests": ["oos"],
        "cost_model": "zero",
    }
    body.update(overrides)
    return LabRunRequest(**body)


def _settings(**lab) -> Settings:
    return Settings(lab=lab) if lab else Settings()


def test_preflight_warnings_ride_on_the_result_view(lake_trending):
    execution = execute_lab_run(_settings(), BuyAndHold, _request(), lake=lake_trending)
    view = execution.view()
    assert view.preflight is not None and view.preflight.ok
    codes = {i.code: i for i in view.preflight.issues}
    assert codes["missing_data"].severity == "warning"
    assert "NOPE.US" in codes["missing_data"].message


def test_strict_preflight_from_the_request_stops_the_run(lake_trending):
    with pytest.raises(ValidationError, match="missing_data"):
        execute_lab_run(
            _settings(), BuyAndHold, _request(strict_preflight=True), lake=lake_trending
        )


def test_strict_preflight_from_the_config_stops_the_run(lake_trending):
    with pytest.raises(ValidationError, match="preflight"):
        execute_lab_run(
            _settings(strict_preflight=True), BuyAndHold, _request(), lake=lake_trending
        )


def test_preflight_can_be_turned_off(lake_trending):
    off_by_config = execute_lab_run(
        _settings(preflight=False), BuyAndHold, _request(), lake=lake_trending
    )
    assert off_by_config.view().preflight is None
    off_by_request = execute_lab_run(
        _settings(), BuyAndHold, _request(preflight=False), lake=lake_trending
    )
    assert off_by_request.view().preflight is None

"""Roadmap 23.9: live calibration per model version (Brier, reliability)."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from stonks.core.forecasts import ProbabilityForecast
from stonks.lifecycle.calibration import calibration_report, track_calibration
from stonks.registry.store import StrategyRegistry
from stonks.store.state import SqliteState
from tests.fixtures.lifecycle import MeanFit

DAY = date(2026, 3, 2)


class Forecaster:
    """One event per ticker per week, 0.8 each, resolving two days later as
    ``outcomes[ticker]``."""

    def __init__(self, outcomes: dict[str, bool], p: float = 0.8) -> None:
        self.outcomes = outcomes
        self.p = p

    def forecast_probability(self, ticker, as_of, lake):
        if ticker not in self.outcomes:
            return None
        week = as_of.isocalendar().week
        return ProbabilityForecast(event_key=f"w{week}", probability=self.p)

    def forecast_outcome(self, ticker, event_key, as_of, lake):
        return self.outcomes[ticker] if as_of >= DAY + timedelta(days=2) else None


class Broken(Forecaster):
    def forecast_probability(self, ticker, as_of, lake):
        raise RuntimeError("boom")


@pytest.fixture
def env(tmp_path):
    state = SqliteState(tmp_path / "state.sqlite")
    state.migrate()
    registry = StrategyRegistry(state=state, artifacts_dir=tmp_path / "artifacts")
    sid = registry.register(MeanFit({"ticker": "UP.US"}), reports=[])
    yield state, sid
    state.close()


def test_records_once_per_event_and_resolves(env) -> None:
    state, sid = env
    model = Forecaster({"A.US": True, "B.US": False})
    first = track_calibration(state, None, [(sid, 2, model)], ["A.US", "B.US", "C.US"], DAY)
    assert first == [{"strategy_id": sid, "version": 2, "recorded": 2, "resolved": 0}]
    again = track_calibration(state, None, [(sid, 2, model)], ["A.US", "B.US"], DAY)
    assert again[0]["recorded"] == 0  # same events, kept as first seen
    report = calibration_report(state, sid, 2)
    assert report.n_forecasts == 2 and report.n_resolved == 0 and report.brier is None

    later = track_calibration(state, None, [(sid, 2, model)], [], DAY + timedelta(days=2))
    assert later[0]["resolved"] == 2
    report = calibration_report(state, sid, 2)
    # (0.8-1)^2 and (0.8-0)^2, base rate 0.5
    assert report.brier == pytest.approx((0.04 + 0.64) / 2)
    assert report.brier_base_rate == pytest.approx(0.25)
    assert report.skill == pytest.approx(1 - 0.34 / 0.25)
    assert report.ece == pytest.approx(0.3)
    top = report.bins[8]
    assert top.count == 2 and top.observed_rate == 0.5
    assert report.as_dict()["bins"][8]["count"] == 2


def test_versions_are_kept_apart(env) -> None:
    state, sid = env
    track_calibration(state, None, [(sid, 1, Forecaster({"A.US": True}))], ["A.US"], DAY)
    assert calibration_report(state, sid, 1).n_forecasts == 1
    assert calibration_report(state, sid, 2).n_forecasts == 0


def test_non_forecasters_and_failures_are_skipped(env) -> None:
    state, sid = env
    out = track_calibration(
        state, None, [(sid, 1, object()), (sid, 2, Broken({"A.US": True}))], ["A.US"], DAY
    )
    assert out == []
    assert calibration_report(state, sid, 2).n_forecasts == 0

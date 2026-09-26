"""LabRunner calls the BL-37 preflight before tuning."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from stonks.core.interval import Interval
from stonks.lab.dataset import LabDataset
from stonks.lab.preflight import PreflightError
from stonks.lab.runner import LabRunner
from stonks.lab.survival.base import SurvivalSuite
from stonks.lab.trials import TrialLedger
from stonks.store.state import SqliteState
from stonks.strategies.examples.buy_and_hold import BuyAndHold


class _Tuned:
    def __init__(self):
        self.best_params = {"ticker": "A.US", "allocation": 0.5}
        self.best_score = 0.1
        self.history = [(dict(self.best_params), 0.1)]
        self.trials = None


class _Tuner:
    seed = 1

    def __init__(self):
        self.calls = 0

    def tune(self, strategy_cls, param_space, objective, dataset, budget):
        self.calls += 1
        return _Tuned()


class _Objective:
    name = "fake"
    direction = "maximize"

    def score(self, strategy, dataset):
        return 0.0


def _bars(lake, ticker):
    days = pd.bdate_range("2023-06-01", periods=280)
    lake.upsert_bars(
        pd.DataFrame(
            {
                "ticker": ticker,
                "timestamp": days,
                "open": 10.0,
                "high": 10.0,
                "low": 10.0,
                "close": 10.0,
                "adj_close": 10.0,
                "volume": 100,
            }
        ),
        Interval.DAY_1,
    )


def _ds(lake, universe=("A.US",)):
    return LabDataset(
        lake=lake,
        universe=list(universe),
        start=date(2024, 1, 1),
        end=date(2024, 6, 28),
        benchmark="none",
    )


def _runner(tuner, **kw):
    return LabRunner(tuner=tuner, objective=_Objective(), suite=SurvivalSuite([]), budget=1, **kw)


def test_warnings_are_recorded_and_the_run_goes_ahead(lake):
    _bars(lake, "A.US")
    tuner = _Tuner()
    result = _runner(tuner).run(BuyAndHold, _ds(lake))
    assert tuner.calls == 1
    codes = {i.code for i in result.preflight.issues}
    assert {"static_universe", "zero_costs"} <= codes
    assert result.manifest["preflight"]["ok"] is True


def test_a_fatal_issue_stops_the_run_before_tuning(lake, tmp_path):
    state = SqliteState(tmp_path / "state.sqlite")
    state.migrate()
    ledger = TrialLedger(state, tmp_path / "artifacts")
    tuner = _Tuner()
    with pytest.raises(PreflightError, match="no_data"):
        _runner(tuner, ledger=ledger).run(BuyAndHold, _ds(lake))
    assert tuner.calls == 0
    assert ledger.runs() == []  # nothing pre-registered for a run that never started
    state.close()


def test_strict_mode_fails_on_warnings(lake):
    _bars(lake, "A.US")
    with pytest.raises(PreflightError, match="static_universe"):
        _runner(_Tuner(), strict_preflight=True).run(BuyAndHold, _ds(lake))


def test_preflight_can_be_turned_off(lake):
    tuner = _Tuner()
    result = _runner(tuner, preflight=False).run(BuyAndHold, _ds(lake))
    assert tuner.calls == 1
    assert result.preflight is None
    assert "preflight" not in result.manifest


def test_a_crashing_preflight_never_blocks_the_run():
    class _BrokenLake:
        def bar_coverage(self, *a, **k):
            raise RuntimeError("disk on fire")

    ds = LabDataset(
        lake=_BrokenLake(), universe=["A.US"], start=date(2024, 1, 1), end=date(2024, 6, 1)
    )
    tuner = _Tuner()
    result = _runner(tuner).run(BuyAndHold, ds)
    assert tuner.calls == 1
    assert result.preflight is None
    assert "disk on fire" in result.manifest["preflight"]["error"]

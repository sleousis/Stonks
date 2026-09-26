"""Integration 1: lab runs through the API service record the trial ledger
and manifest, attach provenance to registered artifacts, tune in parallel
while staying cancellable, warn on zero costs, and backtests return the
trade ledger (W1.1, W1.2, W1.3, BL-13, roadmap 11.3)."""

from __future__ import annotations

import json
import pickle
from datetime import date
from pathlib import Path

import pytest

import stonks.lab.runner as runner_module
import stonks.lab.tuning.base as tuning_base
from stonks.app.jobs import JobCancelled, JobContext
from stonks.app.lab import BacktestRequest, LabRunRequest, _CancellableObjective
from stonks.app.strategies import StrategyRef
from stonks.backtest.costs import CostModelSettings
from stonks.lab.objectives import SharpeObjective
from stonks.lab.parallel import ParallelSettings

MOMENTUM = "stonks.strategies.examples.momentum:Momentum"


def _request(**extra) -> LabRunRequest:
    base = {
        "strategy": StrategyRef(class_path=MOMENTUM),
        "universe": ["UP.US", "DOWN.US"],
        "start": date(2025, 10, 1),
        "end": date(2026, 4, 1),
        "budget": 3,
        "survival_tests": ["oos"],
    }
    return LabRunRequest(**(base | extra))


class _Spy:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict]] = []

    def info(self, event, **kw):
        self.events.append(("info", event, kw))

    def warning(self, event, **kw):
        self.events.append(("warning", event, kw))

    def names(self, level: str) -> list[str]:
        return [e for lvl, e, _ in self.events if lvl == level]


class _Store:
    def set_progress(self, *args) -> None:
        pass


def test_lab_run_records_ledger_and_hypothesis(services, settings):
    view = services.lab.run_lab(
        _request(hypothesis="trend persists after news", premortem="chop kills it")
    )
    assert view.run_id and view.n_trials_run == 3 and view.n_trials_class == 3
    with services.context.state() as state:
        (run,) = state.sql("SELECT * FROM lab_runs WHERE id = ?", [view.run_id])
        n = state.sql("SELECT COUNT(*) AS n FROM lab_trials WHERE run_id = ?", [view.run_id])
    assert run["hypothesis"] == "trend persists after news"
    assert run["premortem"] == "chop kills it"
    assert run["verdict"] == view.verdict
    assert n[0]["n"] == 3
    manifest = json.loads(run["manifest_json"])
    assert manifest["costs"] is not None
    # a second run of the class adds to the class count (P2)
    again = services.lab.run_lab(_request())
    assert again.n_trials_class == 6


def test_registered_artifact_carries_lab_provenance(services, settings):
    view = services.lab.run_lab(_request(register_strategy=True, hypothesis="h1"))
    with services.context.state() as state:
        (row,) = state.sql(
            "SELECT artifact_path FROM strategies WHERE id = ?", [view.registered_strategy_id]
        )
    # Stored relative to the artifacts folder (TO-09).
    artifact = Path(settings.registry.artifacts_dir) / row["artifact_path"]
    meta = json.loads((artifact / "meta.json").read_text())
    assert meta["lab_run_id"] == view.run_id
    assert meta["hypothesis"] == "h1"
    assert meta["n_trials_total"] == view.n_trials_class
    assert meta["class_path"].endswith(":Momentum")  # registry keys survive


def test_register_if_passes_never_registers_a_failed_run(services, monkeypatch):
    import stonks.app.lab as lab_module
    from stonks.core.protocols import SurvivalReport

    class _Fail:
        id = "oos"

        def run(self, strategy, context):
            return SurvivalReport(test_id="oos", passed=False, metrics={})

    monkeypatch.setattr(lab_module, "build_survival_test", lambda name, options=None: _Fail())
    before = services.strategies.counts().total
    view = services.lab.run_lab(_request(register_if_passes=True))
    assert view.verdict == "fail" and view.registered_strategy_id is None
    assert services.strategies.counts().total == before


def test_cancellable_objective_ships_its_inner_objective_to_workers():
    ctx = JobContext(job_id="j1", _store=_Store())
    wrapped = _CancellableObjective(SharpeObjective(), ctx)
    assert isinstance(pickle.loads(pickle.dumps(wrapped.worker_objective)), SharpeObjective)
    assert callable(getattr(wrapped, "evaluate", None))  # trial matrix kept in-process
    wrapped.checkpoint()
    ctx.request_cancel()
    with pytest.raises(JobCancelled):
        wrapped.checkpoint()


def test_api_lab_runs_tune_in_parallel_deterministically(services, settings, monkeypatch):
    spy = _Spy()
    monkeypatch.setattr(tuning_base, "_log", spy)
    ctx = JobContext(job_id="j2", _store=_Store())
    settings.lab.parallel = ParallelSettings(max_workers=1)
    serial = services.lab.run_lab(_request(), progress=ctx)
    settings.lab.parallel = ParallelSettings(max_workers=2)
    parallel = services.lab.run_lab(_request(), progress=ctx)
    assert "tuner.parallel.unavailable" not in spy.names("warning")
    assert "random.parallel.unavailable" not in spy.names("warning")
    assert parallel.best_params == serial.best_params
    assert parallel.best_score == serial.best_score
    assert [r.metrics for r in parallel.survival_reports] == [
        r.metrics for r in serial.survival_reports
    ]


def test_zero_cost_lab_run_logs_a_warning(services, monkeypatch):
    spy = _Spy()
    monkeypatch.setattr(runner_module, "_log", spy)
    services.lab.run_lab(_request(cost_model="zero"))
    assert "lab.zero_costs" in spy.names("warning")
    spy.events.clear()
    services.lab.run_lab(_request())  # realistic by default (BL-13)
    assert "lab.zero_costs" not in spy.names("warning")


def test_default_costs_are_realistic(settings):
    assert settings.backtest.costs == CostModelSettings.realistic()


def test_backtest_result_has_trades_drawdown_and_metrics(services):
    request = BacktestRequest(
        strategy=StrategyRef(class_path=MOMENTUM, params={"lookback_days": 5}),
        universe=["UP.US", "DOWN.US"],
        start=date(2025, 10, 1),
        end=date(2026, 4, 1),
    )
    result = services.lab.run_backtest(request)
    assert len(result.drawdown) == len(result.equity)
    assert all(p.value <= 0 for p in result.drawdown)
    assert min(p.value for p in result.drawdown) == pytest.approx(result.max_drawdown)
    stats = result.trade_stats
    assert stats is not None
    assert result.trade_count == stats.n_trades
    assert len(result.trades) == stats.n_trades + stats.n_open
    assert stats.costs_paid > 0  # realistic default costs
    assert result.sortino is not None and result.fitness is not None

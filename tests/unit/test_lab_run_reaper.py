"""Runs that ended with no trials still count their budget (P2), and runs
left without a verdict past the lease are closed by a reaper."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from datetime import date as _date

import pytest

from stonks.lab.offload.queue import LabQueue
from stonks.lab.trials import LAB_RUN_STOPPED_MESSAGE, LabRunSpec, TrialLedger, TrialRecord
from stonks.store.state import SqliteState


@pytest.fixture
def state_path(tmp_path):
    path = tmp_path / "state.sqlite"
    with SqliteState(path) as s:
        s.migrate()
    return path


@pytest.fixture
def ledger(state_path, tmp_path):
    state = SqliteState(state_path)
    yield TrialLedger(state, tmp_path / "artifacts")
    state.close()


def _trials(n: int) -> list[TrialRecord]:
    return [TrialRecord(i, {"k": i}, 0.1 * i) for i in range(n)]


def _age(ledger: TrialLedger, run_id: str, seconds: float) -> None:
    old = (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat(timespec="microseconds")
    ledger.state.execute(
        "UPDATE lab_runs SET started_at = ?, heartbeat_at = NULL WHERE id = ?", [old, run_id]
    )


# ---- unscored budget ----------------------------------------------------------------


def test_a_finished_run_without_trials_counts_its_budget(ledger):
    ledger.record_run(LabRunSpec("a:A", budget=3), _trials(3), verdict="pass")
    crashed = ledger.start_run(LabRunSpec("a:A", budget=7))
    ledger.finish_run(crashed, "error", error="boom")
    assert ledger.n_trials("a:A") == 3 + 7


def test_a_run_still_going_is_not_charged_yet(ledger):
    ledger.start_run(LabRunSpec("a:A", budget=7))
    assert ledger.n_trials("a:A") == 0


def test_a_run_with_some_trials_counts_its_trials(ledger):
    run = ledger.start_run(LabRunSpec("a:A", budget=10))
    ledger.record_trials(run, _trials(8))
    ledger.finish_run(run, "error")
    assert ledger.n_trials("a:A") == 8


def test_family_and_searched_counts_charge_the_budget_too(ledger):
    lost = ledger.start_run(LabRunSpec("a:A", budget=5, family="rs_1"))
    ledger.finish_run(lost, "error")
    ledger.record_run(LabRunSpec("b:B", family="rs_1"), _trials(2), verdict="fail")
    assert ledger.n_trials_family("rs_1") == 7
    assert ledger.n_trials_searched("b:B", "rs_1") == 7


def test_count_unscored_works_for_every_run(ledger):
    lost = ledger.start_run(LabRunSpec("a:A", budget=4))
    ledger.finish_run(lost, "error")
    assert ledger.count_unscored() == 4
    assert [t.status for t in ledger.trials(lost)] == ["failed"] * 4
    assert ledger.n_trials("a:A") == 4  # never twice
    assert ledger.count_unscored() == 0


# ---- reaper -------------------------------------------------------------------------


def test_the_reaper_closes_runs_left_open_past_the_lease(ledger):
    old = ledger.start_run(LabRunSpec("a:A", budget=6))
    _age(ledger, old, 600)
    fresh = ledger.start_run(LabRunSpec("a:A", budget=9))
    assert ledger.reap_unfinished(lease_seconds=120) == [old]
    run = ledger.run(old)
    assert run["verdict"] == "error" and run["finished_at"]
    assert run["error"] == LAB_RUN_STOPPED_MESSAGE and run["error_detail"] is None
    assert ledger.run(fresh)["verdict"] is None
    assert ledger.n_trials("a:A") == 6  # the reaped run's budget is charged
    assert ledger.reap_unfinished(lease_seconds=120) == []


def test_a_run_with_a_recent_heartbeat_is_not_reaped(ledger):
    live = ledger.start_run(LabRunSpec("a:A", budget=6))
    _age(ledger, live, 600)
    ledger.touch(live)
    assert ledger.reap_unfinished(lease_seconds=120) == []


def test_the_queue_reaper_also_closes_stale_lab_runs(ledger, state_path):
    old = ledger.start_run(LabRunSpec("a:A", budget=2))
    _age(ledger, old, 600)
    LabQueue(state_path).reap_stale(120)
    assert ledger.run(old)["verdict"] == "error"


def test_a_lab_runner_reaps_stale_runs_when_it_starts(ledger):
    from stonks.core.protocols import TunerResult
    from stonks.lab.dataset import LabDataset
    from stonks.lab.runner import LabRunner
    from stonks.lab.survival.base import SurvivalSuite
    from stonks.strategies.examples.buy_and_hold import BuyAndHold

    class _Tuner:
        def tune(self, strategy_cls, param_space, objective, dataset, budget):
            p = {"ticker": "X.US", "allocation": 0.5}
            return TunerResult(best_params=p, best_score=1.0, history=[(p, 1.0)])

    class _Objective:
        name = "fake"
        direction = "maximize"

        def score(self, strategy, dataset):
            return 0.0

    old = ledger.start_run(LabRunSpec("x:X", budget=3))
    _age(ledger, old, 3600)
    ds = LabDataset(lake=None, universe=["X.US"], start=_date(2024, 1, 1), end=_date(2024, 6, 1))
    LabRunner(_Tuner(), _Objective(), SurvivalSuite([]), budget=1, ledger=ledger).run(
        BuyAndHold, ds
    )
    assert ledger.run(old)["verdict"] == "error"

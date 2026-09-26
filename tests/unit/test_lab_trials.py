"""TrialLedger: lab runs, their trials and per-bar trial returns (BL-04)."""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd
import pytest

from stonks.core.protocols import TunerResult
from stonks.lab.trials import (
    LabRunSpec,
    TrialLedger,
    TrialMatrix,
    TrialRecord,
    trials_from_tuning,
)
from stonks.store.state import SqliteState


@pytest.fixture
def ledger(tmp_path):
    state = SqliteState(tmp_path / "state.sqlite")
    state.migrate()
    yield TrialLedger(state, tmp_path / "artifacts")
    state.close()


def _spec(cls: str = "pkg.mod:Strat", **kw) -> LabRunSpec:
    return LabRunSpec(strategy_class=cls, **kw)


def _trials(n: int) -> list[TrialRecord]:
    return [TrialRecord(i, {"lookback": 10 + i}, 0.1 * i, n_bars=50) for i in range(n)]


def test_migration_creates_the_tables(ledger):
    tables = ledger.state.tables()
    assert "lab_runs" in tables and "lab_trials" in tables


def test_start_run_records_the_pre_registration_before_any_trial(ledger):
    run_id = ledger.start_run(
        _spec(hypothesis="momentum persists", premortem="it is just beta", budget=5, seed=3)
    )
    run = ledger.run(run_id)
    assert run["hypothesis"] == "momentum persists"
    assert run["premortem"] == "it is just beta"
    assert run["budget"] == 5 and run["seed"] == 3
    assert run["started_at"] and run["finished_at"] is None and run["verdict"] is None
    assert ledger.trials(run_id) == []


def test_run_ids_are_unique_and_sortable(ledger):
    ids = [ledger.start_run(_spec()) for _ in range(3)]
    assert len(set(ids)) == 3
    assert all(i.startswith("lab_") for i in ids)


def test_record_run_writes_n_trial_rows_and_a_t_by_n_matrix(ledger):
    idx = pd.date_range("2024-01-01", periods=50, freq="D")
    values = np.random.default_rng(0).normal(0, 0.01, (50, 4))
    run_id = ledger.record_run(
        _spec(), _trials(4), matrix=TrialMatrix(idx.values, values), verdict="pass"
    )
    assert len(ledger.trials(run_id)) == 4
    m = ledger.trial_matrix(run_id)
    assert m.values.shape == (50, 4)
    assert m.values.dtype == np.float32
    assert (m.index == idx.values).all()
    np.testing.assert_allclose(m.values, values.astype(np.float32))
    assert (ledger.trials_dir / f"{run_id}.npz").exists()
    run = ledger.run(run_id)
    assert run["verdict"] == "pass" and run["finished_at"]


def test_trials_round_trip(ledger):
    run_id = ledger.record_run(_spec(), _trials(2))
    got = ledger.trials(run_id)
    assert got == _trials(2)


def test_failed_trials_are_stored_as_nan_with_status_failed(ledger):
    trials = [
        TrialRecord(0, {"a": 1}, 0.5),
        TrialRecord(1, {"a": 2}, float("nan")),
    ]
    run_id = ledger.record_run(_spec(), trials)
    got = ledger.trials(run_id)
    assert got[0].status == "ok"
    assert got[1].status == "failed" and math.isnan(got[1].score)


def test_no_matrix_means_scores_only(ledger):
    run_id = ledger.record_run(_spec(), _trials(3))
    assert ledger.trial_matrix(run_id) is None


def test_two_runs_of_one_class_count_2n(ledger):
    ledger.record_run(_spec("a:A"), _trials(5))
    ledger.record_run(_spec("a:A"), _trials(5))
    ledger.record_run(_spec("b:B"), _trials(7))
    assert ledger.n_trials("a:A") == 10
    assert ledger.n_trials("b:B") == 7
    assert ledger.n_trials("c:C") == 0


def test_runs_lists_a_class_newest_first(ledger):
    first = ledger.record_run(_spec("a:A"), _trials(1))
    second = ledger.record_run(_spec("a:A"), _trials(1))
    assert [r["id"] for r in ledger.runs("a:A")] == [second, first]


def test_dataset_and_manifest_round_trip_as_json(ledger):
    run_id = ledger.start_run(
        _spec(dataset={"universe": ["X.US"]}, manifest={"git_sha": "abc", "seeds": {"tuner": 1}})
    )
    run = ledger.run(run_id)
    assert run["dataset"] == {"universe": ["X.US"]}
    assert run["manifest"]["git_sha"] == "abc"


def test_finish_run_sets_the_verdict(ledger):
    run_id = ledger.start_run(_spec())
    ledger.finish_run(run_id, "error")
    assert ledger.run(run_id)["verdict"] == "error"


def test_unknown_run_is_none(ledger):
    assert ledger.run("nope") is None
    assert ledger.trial_matrix("nope") is None


# ---- trials_from_tuning ----------------------------------------------------------


@dataclass
class _Tuned:  # a TunerResult carrying BL-07's duck-typed ``trials``
    best_params: dict
    best_score: float
    history: list
    trials: list


@dataclass
class _Outcome:  # the shape BL-07's TrialOutcome will have
    score: float
    returns: object
    n_bars: int


def test_trials_from_tuning_without_outcomes_uses_history_only():
    tuned = TunerResult(
        best_params={"a": 1}, best_score=0.5, history=[({"a": 1}, 0.5), ({"a": 2}, float("nan"))]
    )
    records, matrix = trials_from_tuning(tuned)
    assert [r.trial_index for r in records] == [0, 1]
    assert records[1].status == "failed"
    assert records[0].n_bars is None
    assert matrix is None


def test_trials_from_tuning_builds_the_matrix_from_outcomes():
    idx = pd.date_range("2024-01-01", periods=5, freq="D")
    tuned = _Tuned(
        best_params={"a": 1},
        best_score=0.5,
        history=[({"a": 1}, 0.5), ({"a": 2}, float("nan"))],
        trials=[_Outcome(0.5, pd.Series([0.01, 0.02, 0.0, -0.01, 0.03], index=idx), 5), None],
    )
    records, matrix = trials_from_tuning(tuned)
    assert records[0].n_bars == 5
    assert matrix.values.shape == (5, 2)
    assert np.isnan(matrix.values[:, 1]).all()
    assert (matrix.index == idx.values).all()


def test_trials_from_tuning_aligns_series_on_their_dates():
    a = pd.Series([0.01, 0.02], index=pd.to_datetime(["2024-01-01", "2024-01-02"]))
    b = pd.Series([0.03], index=pd.to_datetime(["2024-01-02"]))
    tuned = _Tuned({}, 0.0, [({}, 0.1), ({}, 0.2)], [_Outcome(0.1, a, 2), _Outcome(0.2, b, 1)])
    _, matrix = trials_from_tuning(tuned)
    assert matrix.values.shape == (2, 2)
    assert np.isnan(matrix.values[0, 1])
    assert matrix.values[1, 1] == pytest.approx(0.03)


def test_trials_from_tuning_pads_plain_arrays_by_position():
    outcomes = [_Outcome(0.1, np.array([0.1, 0.2, 0.3]), 3), _Outcome(0.2, np.array([0.1]), 1)]
    tuned = _Tuned({}, 0.0, [({}, 0.1), ({}, 0.2)], outcomes)
    _, matrix = trials_from_tuning(tuned)
    assert matrix.values.shape == (3, 2)
    assert (matrix.index == np.arange(3)).all()
    assert np.isnan(matrix.values[1:, 1]).all()

"""Trial families (roadmap 22.9): the lab runs of one research session.

A search across strategy classes is still one search, so a run is judged
against the larger of its class's trial count and its family's (P2)."""

from __future__ import annotations

import numpy as np
import pytest

from stonks.lab.survival.deflated_sharpe import DeflatedSharpeTest
from stonks.lab.trials import LabRunContext, LabRunSpec, TrialLedger, TrialMatrix, TrialRecord
from stonks.store.state import SqliteState


@pytest.fixture
def ledger(tmp_path):
    state = SqliteState(tmp_path / "state.sqlite")
    state.migrate()
    yield TrialLedger(state, tmp_path / "artifacts")
    state.close()


def _trials(n: int) -> list[TrialRecord]:
    return [TrialRecord(i, {"lookback": 10 + i}, 0.1 * i, n_bars=50) for i in range(n)]


def test_a_family_counts_trials_across_classes(ledger):
    ledger.record_run(LabRunSpec("a:A", family="rs_1"), _trials(3))
    ledger.record_run(LabRunSpec("b:B", family="rs_1"), _trials(4))
    ledger.record_run(LabRunSpec("b:B", family="rs_2"), _trials(5))
    ledger.record_run(LabRunSpec("b:B"), _trials(6))
    assert ledger.n_trials_family("rs_1") == 7
    assert ledger.n_trials_family("rs_2") == 5
    assert ledger.n_trials_family("rs_none") == 0
    assert ledger.n_trials("b:B") == 15
    assert ledger.run(ledger.runs("a:A")[0]["id"])["family"] == "rs_1"


def test_unscored_runs_of_a_family_are_listed(ledger):
    done = ledger.record_run(LabRunSpec("a:A", family="rs_1", budget=3), _trials(3))
    lost = ledger.start_run(LabRunSpec("a:A", family="rs_1", budget=9))
    assert ledger.unscored_runs("rs_1") == [(lost, 9)]
    assert done not in [r for r, _ in ledger.unscored_runs("rs_1")]


def test_a_stopped_run_counts_its_whole_budget_as_failed_trials(ledger):
    lost = ledger.start_run(LabRunSpec("a:A", family="rs_1", budget=4))
    assert ledger.count_unscored("rs_1") == 4
    assert ledger.n_trials_family("rs_1") == 4
    assert [t.status for t in ledger.trials(lost)] == ["failed"] * 4
    assert ledger.unscored_runs("rs_1") == []
    assert ledger.count_unscored("rs_1") == 0


def test_deflated_sharpe_uses_the_family_count_when_it_is_larger():
    rng = np.random.default_rng(0)
    matrix = TrialMatrix(index=np.arange(200), values=rng.normal(0, 0.01, (200, 5)))
    test = DeflatedSharpeTest()
    ctx = LabRunContext(
        setup=None,  # type: ignore[arg-type]
        run_id="r",
        ledger=None,
        trials=_trials(5),
        trial_matrix=matrix,
        n_trials_run=5,
        n_trials_class=5,
        n_trials_family=40,
    )
    test.bind_run(ctx)
    assert test.trial_count(ctx) == 40
    ctx.n_trials_family = 2
    assert test.trial_count(ctx) == 5


# ---- review 2026-09-27: the class and the family together, each trial once (P2) --------


def test_the_searched_count_is_the_union_of_class_and_family(ledger):
    ledger.record_run(LabRunSpec("a:A"), _trials(100))  # the class, outside the family
    ledger.record_run(LabRunSpec("b:B", family="rs_1"), _trials(50))
    ledger.record_run(LabRunSpec("a:A", family="rs_1"), _trials(10))
    assert ledger.n_trials_searched("a:A", "rs_1") == 160
    assert ledger.n_trials_searched("a:A", None) == 110


def test_deflated_sharpe_judges_against_the_union():
    rng = np.random.default_rng(0)
    matrix = TrialMatrix(index=np.arange(200), values=rng.normal(0, 0.01, (200, 5)))
    test = DeflatedSharpeTest()
    ctx = LabRunContext(
        setup=None,  # type: ignore[arg-type]
        run_id="r",
        ledger=None,
        trials=_trials(5),
        trial_matrix=matrix,
        n_trials_run=5,
        n_trials_class=110,
        n_trials_family=60,
        n_trials_searched=160,
    )
    test.bind_run(ctx)
    assert test.trial_count(ctx) == 160

"""core.protocols.TrialOutcome — one tuning trial's result, as tuners hand
it to the trial ledger — and TunerResult.trials."""

from __future__ import annotations

import pickle

import numpy as np

from stonks.core.protocols import TrialOutcome, TunerResult


def _outcome(**kw) -> TrialOutcome:
    base = {
        "params": {"a": 1},
        "score": 0.5,
        "returns": np.array([0.01, np.nan, -0.02]),
        "index": np.array(["2025-01-02", "2025-01-03", "2025-01-06"], dtype="datetime64[ns]"),
    }
    return TrialOutcome(**{**base, **kw})


def test_n_bars_counts_returns_and_defaults_to_ok():
    outcome = _outcome()
    assert outcome.n_bars == 3
    assert outcome.status == "ok" and outcome.error is None


def test_outcomes_compare_by_value_including_nan_returns():
    assert _outcome() == _outcome()
    assert _outcome() != _outcome(score=0.6)
    assert _outcome() != _outcome(returns=np.array([0.01, np.nan, -0.03]))


def test_nan_scores_compare_equal_so_failed_trials_are_reproducible():
    a = TrialOutcome.failed({"a": 1}, "boom")
    b = TrialOutcome.failed({"a": 1}, "boom")
    assert a == b
    assert np.isnan(a.score) and a.status == "failed" and a.returns is None and a.n_bars == 0


def test_summary_only_outcome_has_no_returns():
    outcome = TrialOutcome(params={}, score=1.0)
    assert outcome.returns is None and outcome.index is None and outcome.n_bars == 0


def test_outcome_pickles():
    outcome = _outcome()
    assert pickle.loads(pickle.dumps(outcome)) == outcome


def test_with_params_replaces_only_params():
    outcome = _outcome().with_params({"b": 2})
    assert outcome.params == {"b": 2} and outcome.score == 0.5 and outcome.n_bars == 3


def test_tuner_result_trials_default_to_none():
    result = TunerResult(best_params={}, best_score=0.0, history=[])
    assert result.trials is None

"""The factor bench (roadmap 23.13): every factor scored on one universe,
labelled alive, reversed or dead after Benjamini-Hochberg, and counted in
the trial ledger (P2)."""

from __future__ import annotations

import math

import pytest

from stonks.factors.base import ExpressionFactor, Provenance
from stonks.factors.bench import (
    BENCH_FAMILY,
    factor_bench,
    label_rows,
    record_bench,
    two_sided_p,
)
from stonks.lab.trials import TrialLedger
from stonks.store.state import SqliteState
from tests.unit.test_factor_tearsheet import TICKERS, Oracle, _request
from tests.unit.test_factor_tearsheet import lake as lake


def _noise() -> ExpressionFactor:
    return ExpressionFactor("noise", "$volume/Ref($volume, 3)", family="test")


def test_two_sided_p():
    assert two_sided_p(0.0) == pytest.approx(1.0)
    assert two_sided_p(1.96) == pytest.approx(0.05, abs=1e-3)
    assert two_sided_p(-1.96) == pytest.approx(0.05, abs=1e-3)
    assert math.isnan(two_sided_p(math.nan))
    assert two_sided_p(math.inf) == 0.0


def test_labels_follow_fdr_and_direction():
    up, down = Oracle(5), Oracle(5)
    down.direction = -1
    weak, empty = _noise(), _noise()
    rows = label_rows(
        [
            (up, 50, 0.3, 6.0, None),
            (down, 50, 0.3, 6.0, None),
            (weak, 50, 0.01, 0.5, None),
            (empty, 0, math.nan, math.nan, None),
        ],
        q=0.1,
    )
    assert [r.label for r in rows] == ["alive", "reversed", "dead", "n/a"]


def test_fdr_runs_over_the_whole_family():
    many = [(_noise(), 50, 0.02, 2.0, None) for _ in range(20)]  # p = 0.0455 each
    assert all(r.label == "alive" for r in label_rows(many[:1], q=0.05))
    # twenty tests with the same p all pass BH too (p <= k q / m at k = m) ...
    assert all(r.label == "alive" for r in label_rows(many, q=0.05))
    # ... but one real signal among noise does not rescue the noise
    mixed = [(Oracle(5), 50, 0.3, 6.0, None)] + [(_noise(), 50, 0.01, 1.2, None) for _ in range(19)]
    labels = [r.label for r in label_rows(mixed, q=0.05)]
    assert labels[0] == "alive" and set(labels[1:]) == {"dead"}


def test_bench_on_a_lake(lake):
    oracle, flipped = Oracle(5), Oracle(5, sign=-1)
    flipped.id = "flipped"
    oracle.provenance = Provenance(
        "O (2020)", published=2020, sample_start=2000, sample_end=2010, reported="r"
    )
    result = factor_bench([oracle, flipped, _noise()], lake, _request(), horizon=5, q=0.1)
    by = {r.factor_id: r for r in result.rows}
    assert by["oracle5"].label == "alive"
    assert by["flipped"].label == "reversed"
    assert by["oracle5"].post_publication_ic == pytest.approx(1.0)
    assert by["flipped"].post_publication_ic is None
    assert result.n_tickers == len(TICKERS)
    assert result.counts()["alive"] == 1
    assert result.to_dict()["counts"]["reversed"] == 1


def test_bench_needs_a_universe(lake):
    result = factor_bench([_noise()], lake, _request(TICKERS[:3]))
    assert result.status == "n/a" and result.rows == []
    with pytest.raises(ValueError, match="q"):
        factor_bench([_noise()], lake, _request(), q=1.5)


def test_every_benched_factor_is_a_trial(lake, tmp_path):
    state = SqliteState(tmp_path / "state.sqlite")
    state.migrate()
    ledger = TrialLedger(state, tmp_path / "artifacts")
    result = factor_bench([Oracle(5), _noise()], lake, _request(), horizon=5)
    first = record_bench(ledger, result)
    assert first.run_id is not None
    assert first.n_trials_family == 2
    second = record_bench(ledger, result)
    assert second.n_trials_family == 4
    assert ledger.n_trials_family(BENCH_FAMILY) == 4
    trials = ledger.trials(first.run_id)
    assert [t.params["factor"] for t in trials] == ["oracle5", "noise"]
    assert ledger.run(first.run_id)["verdict"] == "pass"

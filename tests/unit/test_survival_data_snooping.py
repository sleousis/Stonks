"""The data-snooping survival test (roadmap 23.9): SPA, Reality Check and
Romano-Wolf over a trial family's per-bar returns."""

from __future__ import annotations

import time

import numpy as np
import pandas as pd
import pytest

from stonks.lab.survival import registry
from stonks.lab.survival.data_snooping import DataSnoopingTest, family_returns
from stonks.lab.trials import LabRunContext, LabRunSpec, TrialLedger, TrialMatrix, TrialRecord
from stonks.store.state import SqliteState

DATES = pd.bdate_range("2014-01-01", periods=2500).values


def _noise(t: int, n: int, seed: int = 3) -> np.ndarray:
    return np.random.default_rng(seed).normal(0.0, 0.01, size=(t, n))


def _ctx(
    values: np.ndarray | None,
    *,
    scores: list[float] | None = None,
    ledger: TrialLedger | None = None,
    family: str | None = None,
    run_id: str = "run-1",
) -> LabRunContext:
    n = 0 if values is None else values.shape[1]
    scores = scores if scores is not None else [0.0] * n
    matrix = None if values is None else TrialMatrix(index=DATES[: len(values)], values=values)
    return LabRunContext(
        setup=None,  # type: ignore[arg-type]
        run_id=run_id,
        ledger=ledger,
        trials=[TrialRecord(i, {}, s) for i, s in enumerate(scores)],
        trial_matrix=matrix,
        n_trials_run=n,
        n_trials_class=n,
        family=family,
    )


def _run(ctx: LabRunContext, **options) -> object:
    test = DataSnoopingTest(**options)
    test.bind_run(ctx)
    return test.evaluate()


def test_registered_and_in_the_promotion_preset() -> None:
    assert "data_snooping" in registry.survival_test_names()
    assert "data_snooping" in registry.SUITE_PRESETS["promotion"]


def test_noise_fails() -> None:
    out = _run(_ctx(_noise(400, 40)))
    assert out.passed is False
    assert out.metrics["spa_p"] > 0.05
    assert "SPA p" in out.notes


def test_a_real_edge_passes_and_the_pick_is_named() -> None:
    values = _noise(400, 40)
    values[:, 5] += 0.004
    scores = [0.0] * 40
    scores[5] = 2.0
    out = _run(_ctx(values, scores=scores))
    assert out.passed is True, out.notes
    assert out.metrics["spa_p"] <= 0.05
    assert out.metrics["reality_check_p"] <= 0.05
    assert out.metrics["selected_rejected"] == 1.0
    assert out.metrics["romano_wolf_rejected"] >= 1


def test_no_matrix_fails_for_lack_of_data() -> None:
    out = _run(_ctx(None))
    assert out.passed is False
    assert out.notes.startswith("insufficient data")


def test_unbound_fails_for_lack_of_data() -> None:
    out = DataSnoopingTest().evaluate()
    assert out.passed is False and "bind_run" in out.notes


def test_flat_trials_are_not_usable() -> None:
    out = _run(_ctx(np.zeros((400, 5))))
    assert out.passed is False
    assert out.metrics["n_trials_usable"] == 0


def test_family_trials_are_counted(tmp_path) -> None:
    """A lucky trial in this run is judged against every trial of the
    family: with its many noise siblings it no longer passes."""
    state = SqliteState(tmp_path / "state.sqlite")
    state.migrate()
    ledger = TrialLedger(state, tmp_path / "artifacts")
    rng = np.random.default_rng(0)
    for k in range(4):
        rid = ledger.start_run(LabRunSpec("pkg:Strat", family="fam"))
        vals = rng.normal(0.0, 0.01, size=(400, 50))
        ledger.record_trials(
            rid,
            [TrialRecord(i, {}, 0.0) for i in range(50)],
            TrialMatrix(index=DATES[:400], values=vals),
        )
    own = np.random.default_rng(99).normal(0.0, 0.01, size=(400, 2))
    own[:, 0] += 0.0012  # modest edge
    alone = _run(_ctx(own, scores=[1.0, 0.0]))
    in_family = _run(_ctx(own, scores=[1.0, 0.0], ledger=ledger, family="fam", run_id="mine"))
    state.close()
    assert in_family.metrics["n_family_runs"] == 5
    assert in_family.metrics["n_trials"] == 202
    assert in_family.metrics["spa_p"] > alone.metrics["spa_p"]


def test_family_returns_line_up_by_date() -> None:
    own = TrialMatrix(index=DATES[:5], values=np.ones((5, 1)))
    other = TrialMatrix(index=DATES[2:8], values=np.arange(6, dtype=float)[:, None])
    out = family_returns(own, [other])
    assert out.shape == (5, 2)
    assert np.isnan(out[:2, 1]).all()
    assert out[2:, 1].tolist() == [0.0, 1.0, 2.0]


def test_family_returns_skip_positional_matrices() -> None:
    own = TrialMatrix(index=np.arange(5), values=np.ones((5, 1)))
    other = TrialMatrix(index=DATES[:5], values=np.ones((5, 3)))
    assert family_returns(own, [other]).shape == (5, 1)


def test_fast_enough_for_the_promotion_preset() -> None:
    values = _noise(2500, 300)
    start = time.perf_counter()
    _run(_ctx(values))
    assert time.perf_counter() - start < 10.0


@pytest.mark.parametrize("bad", [{"max_p": 0.0}, {"n_boot": 10}, {"mean_block": 0.5}])
def test_options_are_checked(bad) -> None:
    with pytest.raises(ValueError):
        registry.build_survival_test("data_snooping", bad)

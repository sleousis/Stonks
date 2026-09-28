"""Research rows between the server and a remote lab worker (roadmap 14.9)."""

from __future__ import annotations

import base64

import pytest

from stonks.app.errors import ConflictError, ValidationError
from stonks.core.protocols import SurvivalReport
from stonks.lab.offload.research_sync import (
    ArtifactFile,
    ResearchRows,
    apply_rows,
    build_seed,
    collect_delta,
)
from stonks.lab.trials import LabRunSpec, TrialLedger, TrialRecord
from stonks.registry.store import StrategyRegistry
from stonks.store.state import SqliteState
from stonks.strategies.examples.buy_and_hold import BuyAndHold
from tests.fixtures.governance import seed_status


def _state(path):
    state = SqliteState(path)
    state.migrate()
    return state


def _spec(cls: str = "m:C") -> LabRunSpec:
    return LabRunSpec(strategy_class=cls, tuner="random", objective="sharpe", budget=2, seed=1)


@pytest.fixture
def server(tmp_path):
    """A server state: one ledgered run, one active and one shadow strategy."""
    state = _state(tmp_path / "server.sqlite")
    artifacts = tmp_path / "server_artifacts"
    ledger = TrialLedger(state, artifacts)
    ledger.record_run(
        _spec(),
        [
            TrialRecord(0, {"a": 1}, 0.5, 100, "ok"),
            TrialRecord(1, {"a": 2}, float("nan"), 100, "failed"),
        ],
        verdict="pass",
    )
    registry = StrategyRegistry(state=state, artifacts_dir=artifacts)
    active = registry.register(BuyAndHold({"ticker": "UP.US"}), [], strategy_id="bah_active")
    seed_status(registry, active, "active")
    registry.register(BuyAndHold({"ticker": "DOWN.US"}), [], strategy_id="bah_shadow")
    yield state, artifacts
    state.close()


def test_the_seed_has_the_ledger_and_the_active_strategies_with_their_files(server):
    state, artifacts = server
    seed = build_seed(state, artifacts)
    assert len(seed.lab_runs) == 1 and len(seed.lab_trials) == 2
    assert [s["id"] for s in seed.strategies] == ["bah_active"]
    assert seed.strategies[0]["artifact_path"] == "bah_active"
    assert seed.files and all(f.path.startswith("bah_active/") for f in seed.files)


def test_a_seeded_worker_sends_back_only_what_its_job_wrote(server, tmp_path):
    state, artifacts = server
    seed = build_seed(state, artifacts)
    scratch = _state(tmp_path / "scratch.sqlite")
    scratch_artifacts = tmp_path / "scratch_artifacts"
    apply_rows(scratch, scratch_artifacts, seed, keep_status=True)
    # the seed keeps the ledger counts and loads the active strategy
    assert TrialLedger(scratch, scratch_artifacts).n_trials("m:C") == 2
    registry = StrategyRegistry(state=scratch, artifacts_dir=scratch_artifacts)
    assert [h.id for h in registry.list_active()] == ["bah_active"]
    assert registry.load("bah_active").params["ticker"] == "UP.US"

    # the job: one more run and a registered strategy
    run_id = TrialLedger(scratch, scratch_artifacts).record_run(
        _spec("m:D"), [TrialRecord(0, {"b": 1}, 0.1, 50, "ok")], verdict="fail"
    )
    sid = registry.register(
        BuyAndHold({"ticker": "FLAT.US"}),
        [SurvivalReport(test_id="oos", passed=True, metrics={"sharpe_oos": 1.0})],
    )
    delta = collect_delta(scratch, scratch_artifacts, seed=seed)
    assert [r["id"] for r in delta.lab_runs] == [run_id]
    assert [r["run_id"] for r in delta.lab_trials] == [run_id]
    assert [s["id"] for s in delta.strategies] == [sid]
    assert [r["test_id"] for r in delta.survival_reports] == ["oos"]
    assert {f.path.split("/")[0] for f in delta.files} == {sid}

    applied = apply_rows(state, artifacts, delta)
    assert (applied.lab_runs, applied.lab_trials, applied.strategies) == (1, 1, [sid])
    server_registry = StrategyRegistry(state=state, artifacts_dir=artifacts)
    assert server_registry._get_handle(sid).status == "shadow"
    assert server_registry.load(sid).params["ticker"] == "FLAT.US"
    assert TrialLedger(state, artifacts).n_trials("m:D") == 1
    # a retried upload changes nothing
    again = apply_rows(state, artifacts, delta)
    assert (again.lab_runs, again.lab_trials, again.strategies) == (0, 0, [])
    reports = state.sql("SELECT COUNT(*) FROM survival_reports WHERE strategy_id = ?", [sid])
    assert reports[0][0] == 1
    scratch.close()


def test_an_upload_never_sets_a_status_but_shadow(server):
    state, artifacts = server
    row = {
        "id": "sneaky",
        "class_path": "stonks.strategies.examples.buy_and_hold:BuyAndHold",
        "params_json": "{}",
        "artifact_path": None,
        "status": "active",
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    }
    apply_rows(state, artifacts, ResearchRows(strategies=[row]))
    assert state.sql("SELECT status FROM strategies WHERE id='sneaky'")[0][0] == "shadow"


def test_an_id_taken_by_another_class_is_a_conflict(server):
    state, artifacts = server
    row = {
        "id": "bah_shadow",
        "class_path": "other:Class",
        "params_json": "{}",
        "status": "shadow",
        "created_at": "x",
        "updated_at": "x",
    }
    with pytest.raises(ConflictError):
        apply_rows(state, artifacts, ResearchRows(strategies=[row]))


def _file(path: str) -> ArtifactFile:
    return ArtifactFile(path=path, content_b64=base64.b64encode(b"x").decode())


@pytest.mark.parametrize("bad", ["../escape.txt", "/etc/passwd", "a/../../b", "C:/x", "a\\b"])
def test_file_paths_cannot_leave_the_artifacts_folder(server, bad):
    state, artifacts = server
    with pytest.raises(ValidationError):
        apply_rows(state, artifacts, ResearchRows(files=[_file(bad)]))


def test_files_no_row_claims_are_dropped(server, tmp_path):
    state, artifacts = server
    applied = apply_rows(
        state, artifacts, ResearchRows(files=[_file("bah_active/meta.json"), _file("x/y.bin")])
    )
    assert applied.files == 0
    assert not (artifacts / "x").exists()

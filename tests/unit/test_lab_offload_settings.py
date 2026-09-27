"""``[lab.offload]`` settings and the executor factory (roadmap 14.9)."""

from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path

import pytest
from pydantic import ValidationError

from stonks.config import DEFAULT_CONFIG_PATH, load_settings
from stonks.lab.offload.executor import (
    InProcessLabExecutor,
    WorkerLabExecutor,
    make_lab_executor,
)
from stonks.lab.offload.settings import DEFAULT_OFFLOAD_KINDS, LabOffloadSettings


def test_defaults_keep_everything_in_process():
    s = LabOffloadSettings()
    assert s.executor == "in_process"
    assert tuple(s.kinds) == DEFAULT_OFFLOAD_KINDS


def test_the_checked_in_config_matches_the_defaults(monkeypatch):
    monkeypatch.delenv("STONKS_LAB_EXECUTOR", raising=False)
    assert load_settings(DEFAULT_CONFIG_PATH).lab.offload == LabOffloadSettings()


def test_env_turns_the_worker_on(monkeypatch, tmp_path):
    monkeypatch.setenv("STONKS_LAB_EXECUTOR", "worker")
    assert load_settings(tmp_path / "none.toml").lab.offload.executor == "worker"


def test_an_unknown_executor_is_refused(monkeypatch, tmp_path):
    monkeypatch.setenv("STONKS_LAB_EXECUTOR", "cloud")
    with pytest.raises(ValidationError):
        load_settings(tmp_path / "none.toml")


def test_snapshot_root_defaults_next_to_the_lake(tmp_path):
    assert LabOffloadSettings().snapshot_root(tmp_path / "lake.duckdb") == (
        tmp_path / "lab_snapshots"
    )
    custom = LabOffloadSettings(snapshot_dir=Path("/x/snaps"))
    assert custom.snapshot_root(tmp_path / "lake.duckdb") == Path("/x/snaps")


def test_factory_builds_the_named_executor(tmp_path):
    kwargs = {
        "state_path": tmp_path / "state.sqlite",
        "lake_path": tmp_path / "lake.duckdb",
        "open_lake": nullcontext,
    }
    assert isinstance(make_lab_executor(LabOffloadSettings(), **kwargs), InProcessLabExecutor)
    worker = make_lab_executor(LabOffloadSettings(executor="worker"), **kwargs)
    assert isinstance(worker, WorkerLabExecutor)
    assert worker.snapshots.root == tmp_path / "lab_snapshots"


def test_worker_executor_offloads_only_its_kinds_without_a_data_fetch(tmp_path):
    worker = make_lab_executor(
        LabOffloadSettings(executor="worker", kinds=["lab_run"]),
        state_path=tmp_path / "state.sqlite",
        lake_path=tmp_path / "lake.duckdb",
        open_lake=nullcontext,
    )
    assert worker.offloads("lab_run", {"ensure_data": False})
    assert not worker.offloads("lab_run", {"ensure_data": True})
    assert not worker.offloads("backtest", {})
    in_process = InProcessLabExecutor()
    assert not in_process.offloads("lab_run", {})
    assert not in_process.tracks("job_x")
    assert not in_process.request_cancel("job_x")
    in_process.prepare("lab_run", {})  # a no-op

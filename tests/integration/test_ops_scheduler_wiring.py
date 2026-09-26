"""Ops and scheduler wiring: server-side backups (a ``backup`` job kind),
scheduled backups and broker syncs on every backend, the notification
delivery worker, and the ``stonks backup`` / ``stonks schedule`` commands."""

from __future__ import annotations

import json
import threading
from datetime import UTC, date, datetime, timedelta

import httpx2
import pytest
from typer.testing import CliRunner

from stonks.app.context import AppContext
from stonks.app.services import Services
from stonks.config import Settings
from stonks.ops.backup import run_configured_backup
from stonks.scheduling.api_backend import API_ACTIONS, ApiExecutor
from stonks.scheduling.api_client import SchedulerApiClient
from stonks.scheduling.config import SchedulerConfig, default_jobs
from stonks.scheduling.in_process import IN_PROCESS_ACTIONS, InProcessExecutor
from stonks.scheduling.jobs import JobSpec, RunContext, build_job_specs
from stonks.scheduling.local import LOCAL_ACTIONS
from stonks.scheduling.triggers import Fire, IntervalTrigger
from stonks.store.lake import DuckDBLake
from stonks.store.state import SqliteState

AS_OF = date(2026, 9, 25)


@pytest.fixture
def settings(tmp_path) -> Settings:
    s = Settings(
        lake={"path": tmp_path / "data" / "lake.duckdb"},
        state={"path": tmp_path / "data" / "state.sqlite"},
        registry={"artifacts_dir": tmp_path / "data" / "artifacts"},
        notify={"backends": []},
        backup={"dir": tmp_path / "backups"},
    )
    s.lake.path.parent.mkdir(parents=True)
    with DuckDBLake(s.lake.path) as lake:
        lake.migrate()
    with SqliteState(s.state.path) as state:
        state.migrate()
    return s


def _ctx(settings, action: str, executor=None) -> RunContext:
    at = datetime(2026, 9, 25, 21, tzinfo=UTC)
    return RunContext(
        spec=JobSpec(action, action, IntervalTrigger(timedelta(minutes=60))),
        fire=Fire(at, AS_OF, AS_OF.isoformat()),
        run_id="srun_test",
        now=at,
        settings=settings,
        notifier=None,
        executor=executor,
    )


def test_configured_backup_goes_to_the_backup_dir(settings):
    result = run_configured_backup(settings)
    assert (settings.backup.dir / result.ref.id / "manifest.json").exists()


def test_backup_job_runs_in_the_server_process(settings):
    services = Services.create(AppContext(settings))
    services.start()
    try:
        job = services.backups.submit()
        final = services.runner.wait(job.id, timeout=60)
        assert final.status == "succeeded", final.error
        view = services.backups.result(job.id)
        assert (settings.backup.dir / view.backup_id).is_dir()
    finally:
        services.shutdown()


def test_local_backup_and_sync_actions(settings):
    backup = LOCAL_ACTIONS.get("backup")(_ctx(settings, "backup"))
    assert backup.status == "succeeded"
    assert (settings.backup.dir / backup.detail["backup_id"]).is_dir()
    sync = LOCAL_ACTIONS.get("connections_sync")(_ctx(settings, "connections_sync"))
    assert sync.status == "succeeded" and sync.detail["connections"] == 0


def test_in_process_backup_and_sync_actions(settings):
    services = Services.create(AppContext(settings))
    services.start()
    try:
        ex = InProcessExecutor(services, timeout_seconds=60)
        out = IN_PROCESS_ACTIONS.get("backup")(_ctx(settings, "backup", ex))
        assert out.status == "succeeded", out.detail
        assert (settings.backup.dir / out.detail["backup_id"]).is_dir()
        sync = IN_PROCESS_ACTIONS.get("connections_sync")(_ctx(settings, "connections_sync", ex))
        assert sync.status == "succeeded"
    finally:
        services.shutdown()


def test_api_backup_action_starts_the_server_job(settings):
    seen: list[tuple[str, str]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append((request.method, request.url.path))
        if request.method == "POST" and request.url.path == "/api/backups":
            return httpx2.Response(202, json={"id": "job_b", "status": "queued"})
        if request.url.path == "/api/jobs/job_b":
            return httpx2.Response(200, json={"id": "job_b", "status": "succeeded"})
        if request.url.path == "/api/backups/jobs/job_b/result":
            return httpx2.Response(200, json={"backup_id": "stonks-x", "pruned": []})
        return httpx2.Response(404)

    client = SchedulerApiClient(
        "http://127.0.0.1:8000", token="t", transport=httpx2.MockTransport(handler)
    )
    ex = ApiExecutor(client, poll_seconds=0.0)
    out = API_ACTIONS.get("backup")(_ctx(settings, "backup", ex))
    assert out.status == "succeeded" and out.detail["backup_id"] == "stonks-x"
    assert seen[0] == ("POST", "/api/backups")


def test_default_jobs_include_backups_and_syncs_on_every_backend():
    names = {j.action for j in default_jobs()}
    assert {"backup", "connections_sync"} <= names
    config = SchedulerConfig()
    for actions in (LOCAL_ACTIONS, API_ACTIONS, IN_PROCESS_ACTIONS):
        build_job_specs(config, env={}, actions=actions.names())


def test_settings_carry_the_scheduler_config():
    assert Settings().scheduler == SchedulerConfig()
    s = Settings(scheduler={"catch_up": "none", "deliver_notifications": False})
    assert s.scheduler.catch_up == "none" and not s.scheduler.deliver_notifications


def test_delivery_worker_runs_on_its_own_thread_until_stopped(settings, monkeypatch):
    from stonks.scheduling import delivery

    passes: list[str] = []

    class FakeWorker:
        def __init__(self, state, channels, outbox):
            passes.append(threading.current_thread().name)

        def run_forever(self, stop, interval_seconds=5.0):
            stop.wait()

    monkeypatch.setattr(delivery, "DeliveryWorker", FakeWorker)
    handle = delivery.start_delivery_worker(settings)
    assert handle is not None
    handle.stop(timeout=5)
    assert passes == ["stonks-notify-delivery"]
    assert not handle.thread.is_alive()
    off = settings.model_copy(update={"scheduler": SchedulerConfig(deliver_notifications=False)})
    assert delivery.start_delivery_worker(off) is None


def test_cli_mounts_backup_and_schedule(settings, monkeypatch, tmp_path):
    import stonks.cli as cli

    monkeypatch.setattr(cli, "_settings", lambda: settings)
    runner = CliRunner()
    out = runner.invoke(cli.app, ["backup", "--help"])
    assert out.exit_code == 0 and "restore" in out.output and "prune" in out.output
    config = tmp_path / "cfg.toml"
    config.write_text(
        f'[state]\npath = {json.dumps(str(settings.state.path))}\n[scheduler]\nbackend = "local"\n',
        encoding="utf-8",
    )
    out = runner.invoke(cli.app, ["schedule", "--config", str(config), "next"])
    assert out.exit_code == 0, out.output
    assert "backend: local" in out.output and "connections_sync" in out.output
    out = runner.invoke(cli.app, ["schedule", "--config", str(config), "runs"])
    assert out.exit_code == 0, out.output

"""A lab worker on another machine (roadmap 14.9): the queue, the lake
snapshot and the results go through ``/api/lab/worker/*`` with a
``lab_worker`` token. FastAPI's TestClient plays the remote API, so the
worker below never opens the server's state DB or lake."""

from __future__ import annotations

import threading
from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient

from stonks.accounts import Role
from stonks.api import create_app
from stonks.app.context import AppContext
from stonks.app.jobs import JobContext
from stonks.app.lab import LabRunRequest
from stonks.app.services import Services
from stonks.app.strategies import StrategyRef
from stonks.app.sweep import SweepRequest
from stonks.config import LakeConfig, RegistryConfig, StateConfig
from stonks.lab.offload.queue import LabQueue
from stonks.lab.offload.remote_worker import build_remote_worker
from stonks.lab.offload.settings import LabOffloadSettings
from stonks.lab.parallel import ParallelSettings
from stonks.security import KeyRing, SecretBox, generate_key
from stonks.store.state import SqliteState
from tests.integration.auth.helpers import add_user, make_service, session_principal

BASE = "https://testserver"
REMOTE_PEER = ("203.0.113.7", 50000)


def _request(**overrides) -> LabRunRequest:
    base = {
        "strategy": StrategyRef(class_path="stonks.strategies.examples.momentum:Momentum"),
        "universe": ["UP.US", "DOWN.US"],
        "start": date(2025, 10, 1),
        "end": date(2026, 4, 1),
        "tuner": "random",
        "budget": 2,
        "survival_tests": ["oos"],
    }
    base.update(overrides)
    return LabRunRequest(**base)


@pytest.fixture
def offload_settings(settings):
    settings.api.allowed_hosts = ["testserver"]
    settings.lab = settings.lab.model_copy(
        update={
            "parallel": ParallelSettings(max_workers=1),
            "offload": LabOffloadSettings(
                executor="worker", heartbeat_seconds=0.05, poll_seconds=0.05
            ),
        }
    )
    return settings


@pytest.fixture
def auth(settings, seeded):
    box = SecretBox(KeyRing.parse(f"k1:{generate_key()}"))
    return make_service(
        settings.state.path, legacy="test-token-123", box=box, clock=lambda: datetime.now(UTC)
    )


@pytest.fixture
def app(offload_settings, seeded, fake_source, auth):
    svc = Services.create(AppContext(offload_settings, source_factory=lambda: fake_source))
    application = create_app(offload_settings, services=svc)
    application.state.auth = auth
    return application


@pytest.fixture
def api(app) -> Services:
    return app.state.services


@pytest.fixture
def http(app):
    with TestClient(app, client=REMOTE_PEER, base_url=BASE) as c:
        yield c


def _token(auth, settings, email: str, role: Role, scopes: list[str]) -> str:
    uid = add_user(settings.state.path, email, role)
    _, token = auth.create_token(session_principal(uid, role), name="t", scopes=scopes)
    return token


@pytest.fixture
def worker_token(auth, settings) -> str:
    return _token(auth, settings, "worker@example.com", Role.ADMIN, ["lab_worker"])


@pytest.fixture
def remote(offload_settings, http, worker_token, tmp_path):
    """The worker's own install: another data folder, nothing shared."""
    home = tmp_path / "pc"
    own = offload_settings.model_copy(
        update={
            "lake": LakeConfig(path=home / "lake.duckdb"),
            "state": StateConfig(path=home / "state.sqlite"),
            "registry": RegistryConfig(artifacts_dir=home / "artifacts"),
        }
    )
    return build_remote_worker(
        own, api_url=BASE, token=worker_token, worker_id="w-remote", http=http
    )


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---- permissions -------------------------------------------------------------------


def test_a_worker_token_reaches_the_worker_routes_and_nothing_else(http, worker_token):
    headers = _auth(worker_token)
    claim = http.post("/api/lab/worker/claim", json={"worker_id": "w1"}, headers=headers)
    assert claim.status_code == 200 and claim.json() == {"job": None}
    assert http.get("/api/strategies", headers=headers).status_code == 403
    assert http.get("/api/jobs", headers=headers).status_code == 403
    run = http.post("/api/lab/runs", json=_request().model_dump(mode="json"), headers=headers)
    assert run.status_code == 403


def test_a_worker_token_cannot_read_the_auth_routes_either(http, worker_token):
    # "nothing else, not even reads" (docs/security.md): the /api/auth routes
    # sit behind require_token, not authorize, and must confine it too.
    from stonks.auth.credentials import parse_api_token

    headers = _auth(worker_token)
    for path in ("/api/auth/me", "/api/auth/tokens", "/api/auth/toolsets", "/api/auth/check"):
        assert http.get(path, headers=headers).status_code == 403, path
    # revoking itself stays possible: tokens.revoke is granted to every scope
    token_id = parse_api_token(worker_token)
    assert http.delete(f"/api/auth/tokens/{token_id}", headers=headers).status_code == 204
    claim = http.post("/api/lab/worker/claim", json={"worker_id": "w1"}, headers=headers)
    assert claim.status_code == 401


@pytest.mark.parametrize(
    ("role", "scopes"),
    [(Role.TRADER, ["read", "trade", "lab"]), (Role.ADMIN, ["read", "trade", "lab", "admin"])],
)
def test_other_tokens_cannot_act_as_a_worker(http, auth, settings, role, scopes):
    token = _token(auth, settings, f"{role.value}@example.com", role, scopes)
    for path, body in [
        ("/api/lab/worker/register", {"worker_id": "w1", "host": "pc", "pid": 1, "cpus": 2}),
        ("/api/lab/worker/claim", {"worker_id": "w1"}),
        ("/api/lab/worker/stop", {"worker_id": "w1"}),
    ]:
        assert http.post(path, json=body, headers=_auth(token)).status_code == 403, path
    snap = http.get("/api/lab/worker/snapshots/20260101T000000000000Z", headers=_auth(token))
    assert snap.status_code == 403
    assert http.post("/api/lab/worker/claim", json={"worker_id": "w1"}).status_code == 401


def test_a_worker_token_of_a_demoted_admin_reads_nothing(http, auth, settings):
    # Demoting the admin clamps the lab_worker scope away. The token is then
    # left with no scope at all, and must not fall back to plain reads.
    uid = add_user(settings.state.path, "was-admin@example.com", Role.ADMIN)
    _, token = auth.create_token(
        session_principal(uid, Role.ADMIN), name="w", scopes=["lab_worker"]
    )
    with SqliteState(settings.state.path) as state:
        state.execute("UPDATE users SET role = 'trader' WHERE id = ?", [uid])
    headers = _auth(token)
    for path in ("/api/strategies", "/api/universes", "/api/market/instruments", "/api/auth/me"):
        assert http.get(path, headers=headers).status_code == 403, path
    claim = http.post("/api/lab/worker/claim", json={"worker_id": "w1"}, headers=headers)
    assert claim.status_code == 403


def test_a_trader_cannot_mint_a_worker_token(auth, settings):
    from stonks.auth import PermissionDenied

    uid = add_user(settings.state.path, "t@example.com", Role.TRADER)
    with pytest.raises(PermissionDenied):
        auth.create_token(session_principal(uid, Role.TRADER), name="w", scopes=["lab_worker"])


def test_an_unknown_snapshot_is_not_found(http, worker_token):
    resp = http.get("/api/lab/worker/snapshots/20990101T000000000000Z", headers=_auth(worker_token))
    assert resp.status_code == 404


# ---- the queue over the API --------------------------------------------------------


def test_the_remote_worker_runs_a_lab_run_and_registers_the_result(api, remote, settings):
    job = api.lab.submit_lab_run(_request(register_strategy=True))
    assert remote.run_once() == job.id
    done = api.jobs.wait(job.id, timeout=5)
    assert done.status == "succeeded", done.error
    assert [r["test_id"] for r in done.result["survival_reports"]] == ["oos"]
    sid = done.result["registered_strategy_id"]
    assert api.strategies.get(sid).status == "shadow"
    with api.context.registry() as registry:
        assert type(registry.load(sid)).__name__ == "Momentum"
    with SqliteState(settings.state.path) as state:
        run = state.sql("SELECT verdict FROM lab_runs WHERE id = ?", [done.result["run_id"]])
        trials = state.sql(
            "SELECT COUNT(*) FROM lab_trials WHERE run_id = ?", [done.result["run_id"]]
        )
    assert run and trials[0][0] == 2
    assert remote.run_once() is None  # the queue is empty
    stats = LabQueue(settings.state.path).stats(lease_seconds=60)
    assert stats.outcomes["succeeded"] == 1 and stats.workers_alive == 1
    # the scratch stores are gone; the snapshot stays for the next job
    assert not any((remote.work_dir / "jobs").iterdir())
    assert remote.snapshots.current() is not None


def test_the_second_run_counts_the_first_runs_trials(api, remote):
    """P2: the seed carries the ledger, so the trial count grows across
    runs a remote worker ran."""
    first = api.lab.submit_lab_run(_request())
    assert remote.run_once() == first.id
    second = api.lab.submit_lab_run(_request())
    assert remote.run_once() == second.id
    a = api.jobs.wait(first.id, timeout=5).result
    b = api.jobs.wait(second.id, timeout=5).result
    assert b["n_trials_class"] == a["n_trials_class"] + 2


def test_the_remote_worker_runs_a_sweep(api, remote):
    job = api.lab.submit_sweep(
        SweepRequest(
            universe=["UP.US", "DOWN.US"],
            start=date(2025, 10, 1),
            end=date(2026, 4, 1),
            strategies=["momentum"],
            tuner="random",
            budget=1,
            survival_tests=["oos"],
        )
    )
    assert remote.run_once() == job.id
    done = api.jobs.wait(job.id, timeout=5)
    assert done.status == "succeeded", done.error


def test_a_job_naming_a_registered_strategy_waits_for_a_local_worker(api, remote, seeded):
    job = api.lab.submit_lab_run(_request(strategy=StrategyRef(strategy_id=seeded["active_id"])))
    assert remote.run_once() is None
    assert api.jobs.get(job.id).status == "queued"


def test_a_cancel_from_the_api_stops_the_remote_job(api, remote, monkeypatch):
    job = api.lab.submit_lab_run(_request(budget=1000))
    at_checkpoint = threading.Event()
    cancel_sent = threading.Event()
    check_cancelled = JobContext.check_cancelled

    def gated(ctx: JobContext) -> None:
        if ctx.job_id == job.id:
            at_checkpoint.set()
            cancel_sent.wait(timeout=120)
            ctx._cancel.wait(timeout=120)  # the remote heartbeat turns the cancel into this
        check_cancelled(ctx)

    monkeypatch.setattr(JobContext, "check_cancelled", gated)
    stop = threading.Event()
    thread = threading.Thread(target=remote.run_forever, args=(stop,), daemon=True)
    thread.start()
    try:
        assert at_checkpoint.wait(timeout=120), "the worker never reached a trial"
        requested = api.jobs.cancel(job.id)
        assert (requested.status, requested.message) == ("running", "cancellation requested")
        cancel_sent.set()
        done = api.jobs.wait(job.id, timeout=120)
        assert done.status == "cancelled", done.error
    finally:
        cancel_sent.set()
        stop.set()
        thread.join(timeout=120)
    assert not thread.is_alive()
    assert LabQueue(api.context.settings.state.path).stats(lease_seconds=60).workers_alive == 0


def test_a_stopping_remote_worker_requeues_its_job(api, remote, monkeypatch):
    job = api.lab.submit_lab_run(_request(budget=1000))
    at_checkpoint = threading.Event()
    stop_sent = threading.Event()
    check_cancelled = JobContext.check_cancelled

    def gated(ctx: JobContext) -> None:
        if ctx.job_id == job.id:
            at_checkpoint.set()
            stop_sent.wait(timeout=120)
        check_cancelled(ctx)

    monkeypatch.setattr(JobContext, "check_cancelled", gated)
    stop = threading.Event()
    thread = threading.Thread(target=remote.run_forever, args=(stop,), daemon=True)
    thread.start()
    try:
        assert at_checkpoint.wait(timeout=120), "the worker never reached a trial"
        stop.set()
        remote.request_stop()
    finally:
        stop_sent.set()
        stop.set()
        thread.join(timeout=120)
    assert not thread.is_alive()
    requeued = api.jobs.get(job.id)
    assert (requeued.status, requeued.error) == ("queued", None)
    assert LabQueue(api.context.settings.state.path).claim_next("w2", ["lab_run"]) == job.id


def test_a_lost_job_is_reported_to_the_worker_and_not_completed(api, http, worker_token):
    headers = _auth(worker_token)
    job = api.lab.submit_lab_run(_request())
    claim = http.post("/api/lab/worker/claim", json={"worker_id": "w1"}, headers=headers).json()
    assert claim["job"]["id"] == job.id
    assert claim["job"]["seed"]["lab_runs"] == []
    assert [s["id"] for s in claim["job"]["seed"]["strategies"]] == ["bah_active"]
    beat = http.post(
        f"/api/lab/worker/jobs/{job.id}/heartbeat",
        json={"worker_id": "w1", "progress": 0.5, "message": "tuning"},
        headers=headers,
    ).json()
    assert beat == {"running": True, "cancel_requested": False}
    assert api.jobs.get(job.id).message == "tuning"
    # another worker id holds nothing
    other = http.post(
        f"/api/lab/worker/jobs/{job.id}/heartbeat", json={"worker_id": "w2"}, headers=headers
    ).json()
    assert other["running"] is False
    foreign = http.post(
        f"/api/lab/worker/jobs/{job.id}/complete",
        json={"worker_id": "w2", "status": "succeeded", "result": {}},
        headers=headers,
    )
    assert foreign.status_code == 409
    LabQueue(api.context.settings.state.path).reap_stale(lease_seconds=0)
    lost = http.post(
        f"/api/lab/worker/jobs/{job.id}/heartbeat", json={"worker_id": "w1"}, headers=headers
    ).json()
    assert lost["running"] is False
    late = http.post(
        f"/api/lab/worker/jobs/{job.id}/complete",
        json={"worker_id": "w1", "status": "succeeded", "result": {}},
        headers=headers,
    )
    assert late.status_code == 409
    assert api.jobs.get(job.id).status == "failed"


def test_the_worker_config_names_only_the_kinds_a_remote_worker_takes(http, worker_token):
    cfg = http.post(
        "/api/lab/worker/register",
        json={"worker_id": "w1", "host": "pc", "pid": 1, "cpus": 32},
        headers=_auth(worker_token),
    ).json()
    assert sorted(cfg["kinds"]) == ["lab_run", "lab_sweep"]
    assert cfg["lease_seconds"] == 120.0 and cfg["executor"] == "worker"

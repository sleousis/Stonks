"""The AI research loop end to end (roadmap 22.9): the REST routes, a
scripted model, and real lab runs on the seeded lake, each counted in the
trial ledger under the session's family. Nothing is registered."""

from __future__ import annotations

import time
from datetime import UTC, date, datetime

import pytest

from stonks.assistant import guard
from stonks.assistant.fake import FakeChatModel, Script, call
from stonks.assistant.research import FINISH, PROPOSE
from stonks.lab.parallel import ParallelSettings
from stonks.lab.trials import TrialLedger
from stonks.store.state import SqliteState

BASE_URL = "http://model.local/v1"
CUTOFF = date(2025, 12, 31)
MOMENTUM = "stonks.strategies.examples.momentum:Momentum"
HYPOTHESIS = "Recent winners keep rising for weeks because investors underreact to news."
PREMORTEM = "It is only beta to a rising market."


def _proposal(**over) -> dict:
    args = {
        "hypothesis": HYPOTHESIS,
        "premortem": PREMORTEM,
        "class_path": MOMENTUM,
        "start": "2025-10-01",
        "end": "2026-04-01",
        "budget": 3,
        "train_ratio": 0.5,
    }
    args.update(over)
    return args


@pytest.fixture
def model(app) -> FakeChatModel:
    services = app.state.services
    settings = services.context.settings
    settings.assistant.base_url = BASE_URL
    settings.assistant.research.model_cutoff = CUTOFF
    settings.lab.parallel = ParallelSettings(max_workers=1)
    fake = FakeChatModel()
    services.assistant.model_factory = lambda cfg: fake
    return fake


def _wait(client, headers, job_id: str, timeout: float = 120) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}", headers=headers).json()
        if job["status"] in ("succeeded", "failed", "cancelled"):
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish")


def _start(client, headers, **over):
    body = {"goal": "find a momentum edge in these names", "universe": ["UP.US", "DOWN.US"]}
    body.update(over)
    return client.post("/api/assistant/research", json=body, headers=headers)


def test_off_without_a_model_endpoint(client, people):
    resp = _start(client, people["alice"]["headers"])
    assert resp.status_code == 503 and resp.json()["code"] == "not_configured"


def test_off_without_the_model_cutoff(client, people, app):
    app.state.services.context.settings.assistant.base_url = BASE_URL
    resp = _start(client, people["alice"]["headers"])
    assert resp.status_code == 503
    assert "model_cutoff" in resp.json()["detail"]


def test_a_viewer_cannot_start_research(client, people, model):
    resp = _start(client, people["vic"]["headers"])
    assert resp.status_code == 403


def test_a_frozen_assistant_starts_no_session(client, people, model, settings):
    with SqliteState(settings.state.path) as state:
        guard.freeze(state, people["alice"]["id"], 60, "a burst", datetime.now(UTC))
    resp = _start(client, people["alice"]["headers"])
    assert resp.status_code == 409


def test_a_session_runs_ledgered_trials_and_registers_nothing(client, people, model, settings):
    alice = people["alice"]
    model.turns = [
        Script(calls=(call(PROPOSE, _proposal()),)),
        # validation would start before the cutoff: rejected, never run
        Script(calls=(call(PROPOSE, _proposal(end="2026-01-20")),)),
        Script(calls=(call(PROPOSE, _proposal(register_strategy=True)),)),
        Script(calls=(call(FINISH, {"summary": "Momentum did not survive."}),)),
    ]
    with SqliteState(settings.state.path) as state:
        strategies_before = state.sql("SELECT COUNT(*) AS n FROM strategies")[0]["n"]

    resp = _start(client, alice["headers"], max_trials=100_000)
    assert resp.status_code == 202, resp.text
    job = _wait(client, alice["headers"], resp.json()["id"])
    assert job["status"] == "succeeded", job["error"]
    session = job["result"]
    assert session["status"] == "done" and session["summary"] == "Momentum did not survive."
    # a request may only lower the configured budget
    assert session["max_trials"] == settings.assistant.research.max_trials

    detail = client.get(f"/api/assistant/research/{session['id']}", headers=alice["headers"])
    assert detail.status_code == 200
    proposals = detail.json()["proposals"]
    assert [p["status"] for p in proposals] == ["done", "rejected", "rejected"]
    assert "cutoff" in proposals[1]["reason"]
    assert "governance" in proposals[2]["reason"]
    ran = proposals[0]
    assert ran["hypothesis"] == HYPOTHESIS and ran["validation_start"] > CUTOFF.isoformat()
    assert ran["outcome"]["n_trials_family"] == 3
    assert detail.json()["trials_used"] == 3

    with SqliteState(settings.state.path) as state:
        ledger = TrialLedger(state, settings.registry.artifacts_dir)
        run = ledger.run(ran["lab_run_id"])
        assert run["family"] == session["id"]
        assert run["hypothesis"] == HYPOTHESIS and run["premortem"] == PREMORTEM
        assert ledger.n_trials_family(session["id"]) == 3
        assert state.sql("SELECT COUNT(*) AS n FROM strategies")[0]["n"] == strategies_before

    listed = client.get("/api/assistant/research", headers=alice["headers"]).json()
    assert [s["id"] for s in listed["items"]] == [session["id"]]
    # another person's session reads as missing
    other = client.get(f"/api/assistant/research/{session['id']}", headers=people["bob"]["headers"])
    assert other.status_code == 404
    assert client.get("/api/assistant/research", headers=people["bob"]["headers"]).json()[
        "total"
    ] == 0

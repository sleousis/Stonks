"""Roadmap 23.9: the weekly ``lab_verify`` scheduler action on the ``api``,
``in_process`` and ``local`` backends, each against real stores."""

from __future__ import annotations

from datetime import date

from stonks.scheduling.api_backend import API_ACTIONS
from stonks.scheduling.config import default_jobs
from stonks.scheduling.in_process import IN_PROCESS_ACTIONS, InProcessExecutor
from stonks.scheduling.jobs import verify_outcome
from stonks.scheduling.local import LOCAL_ACTIONS, LocalExecutor
from tests.integration.app.test_scheduling_backends import _ctx, api_executor  # noqa: F401

FIRE = date(2026, 3, 22)


def test_every_backend_registers_the_weekly_action():
    for actions in (API_ACTIONS, IN_PROCESS_ACTIONS, LOCAL_ACTIONS):
        assert "lab_verify" in actions.names()
    job = next(j for j in default_jobs() if j.action == "lab_verify")
    assert job.trigger.type == "daily"
    assert job.trigger.weekdays == [6]


def test_outcome_classification():
    assert verify_outcome({"checked": 2, "moved": []}).status == "succeeded"
    moved = verify_outcome({"checked": 2, "moved": ["s1"], "alerted": True}, "job_1")
    assert moved.status == "failed" and moved.detail["moved"] == ["s1"]
    assert moved.detail["job_id"] == "job_1"
    skipped = verify_outcome({"checked": 0, "moved": []})
    assert skipped.status == "skipped" and skipped.detail["reason"] == "nothing_to_verify"


def _checked(out) -> int:
    assert out.status in {"succeeded", "skipped"}, out.detail
    return int(out.detail.get("checked") or 0)


def test_api_backend_verifies_through_the_api(settings, seeded, api_executor):  # noqa: F811
    ctx, _ = _ctx(settings, api_executor, "lab_verify", FIRE)
    out = api_executor.execute(ctx)
    assert _checked(out) >= 1
    assert out.detail["moved"] == []


def test_in_process_backend_verifies_on_the_job_runner(settings, seeded, services):
    ex = InProcessExecutor(services)
    out = ex.execute(_ctx(settings, ex, "lab_verify", FIRE)[0])
    assert _checked(out) >= 1


def test_local_backend_verifies_in_this_process(settings, seeded):
    ex = LocalExecutor()
    out = ex.execute(_ctx(settings, ex, "lab_verify", FIRE)[0])
    assert _checked(out) >= 1

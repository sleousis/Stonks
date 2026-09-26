"""S7: backups on disk over the API: list, verify and a staged restore
(admin, fresh second factor, typed confirmation)."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from tests.integration.app.stepup import allow_step_up


@pytest.fixture
def backup_id(app, settings, tmp_path) -> str:
    settings.backup.dir = tmp_path / "backups"
    return app.state.services.backups.run().backup_id


def _wait(client, job_id: str, headers: dict, timeout: float = 60) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}", headers=headers).json()
        if job["status"] in ("succeeded", "failed", "cancelled"):
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish")


def test_admins_list_backups_on_disk(client, people, backup_id):
    listed = client.get("/api/backups", headers=people["ada"]["headers"])
    assert listed.status_code == 200, listed.text
    [item] = listed.json()
    assert item["id"] == backup_id and item["created_at"]
    assert item["size_bytes"] > 0
    assert client.get("/api/backups", headers=people["alice"]["headers"]).status_code == 403


def test_verify_reports_problems(client, settings, people, backup_id):
    ada = people["ada"]["headers"]
    ok = client.post(f"/api/backups/{backup_id}/verify", headers=ada)
    assert ok.status_code == 200, ok.text
    assert ok.json() == {"backup_id": backup_id, "ok": True, "problems": []}
    state_copy = Path(settings.backup.dir) / backup_id / "state" / "state.sqlite"
    state_copy.write_bytes(state_copy.read_bytes() + b"tampered")
    bad = client.post(f"/api/backups/{backup_id}/verify", headers=ada).json()
    assert bad["ok"] is False and any("state.sqlite" in p for p in bad["problems"])
    missing = client.post("/api/backups/stonks-20990101T000000Z/verify", headers=ada)
    assert missing.status_code == 404


def test_restore_needs_a_step_up_and_the_typed_confirmation(
    app, client, settings, people, backup_id
):
    ada = people["ada"]["headers"]
    url = f"/api/backups/{backup_id}/restore"
    token = client.post(url, json={"confirmation": f"RESTORE {backup_id}"}, headers=ada)
    assert token.status_code == 403 and token.json()["code"] == "step_up_required"
    trader = client.post(
        url, json={"confirmation": f"RESTORE {backup_id}"}, headers=people["alice"]["headers"]
    )
    assert trader.status_code == 403

    allow_step_up(app)
    wrong = client.post(url, json={"confirmation": "restore"}, headers=ada)
    assert wrong.status_code == 422
    live_state = Path(settings.state.path).read_bytes()
    started = client.post(url, json={"confirmation": f"RESTORE {backup_id}"}, headers=ada)
    assert started.status_code == 202, started.text
    job = _wait(client, started.json()["id"], ada)
    assert job["status"] == "succeeded", job["error"]
    result = client.get(f"/api/backups/restores/{job['id']}/result", headers=ada).json()
    data_dir = Path(result["data_dir"])
    assert (data_dir / "state.sqlite").is_file() and (data_dir / "lake.duckdb").is_file()
    assert result["backup_id"] == backup_id and result["next_steps"]
    # The live data is never touched: the restore is staged beside it.
    assert Path(settings.state.path).read_bytes()[:100] == live_state[:100]

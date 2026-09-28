"""Roadmap 22.6 through the REST API: retrain into a candidate, read the
swap check, and swap only with a passing check or an override."""

from __future__ import annotations

import pytest

from stonks.registry.store import StrategyRegistry
from stonks.store.state import SqliteState
from tests.fixtures.lifecycle import MeanFit
from tests.integration.app.test_api import AUTH, _wait_job

LONG_REASON = "owner swap: the refit handles the new regime, see ticket 7"


@pytest.fixture
def ml_id(settings, seeded) -> str:
    with SqliteState(settings.state.path) as state:
        registry = StrategyRegistry(state=state, artifacts_dir=settings.registry.artifacts_dir)
        return registry.register(MeanFit({"ticker": "UP.US"}), reports=[], strategy_id="mf")


def _retrain(client, sid):
    resp = client.post(
        "/api/model-versions/retrain",
        json={"strategy_ids": [sid], "as_of": "2026-03-18", "tickers": ["UP.US"]},
        headers=AUTH,
    )
    assert resp.status_code == 202, resp.text
    job = _wait_job(client, resp.json()["id"])
    assert job["status"] == "succeeded", job
    return client.get(f"/api/model-versions/jobs/{job['id']}/result", headers=AUTH).json()


def test_retrain_check_and_governed_swap(client, ml_id):
    result = _retrain(client, ml_id)
    assert (result["candidates"], result["outcomes"][0]["version"]) == (1, 2)

    versions = client.get(f"/api/strategies/{ml_id}/versions", headers=AUTH).json()["items"]
    assert [(v["version"], v["status"]) for v in versions] == [(1, "live"), (2, "candidate")]
    assert versions[1]["book_id"] == f"{ml_id}@v2"
    candidates = client.get("/api/model-versions/candidates", headers=AUTH).json()["items"]
    assert [c["book_id"] for c in candidates] == [f"{ml_id}@v2"]

    check = client.get(f"/api/strategies/{ml_id}/versions/2/check", headers=AUTH).json()
    assert check["passed"] is False and check["live_version"] == 1
    calib = client.get(f"/api/strategies/{ml_id}/versions/2/calibration", headers=AUTH).json()
    assert (calib["n_forecasts"], calib["brier"], calib["bins"]) == (0, None, [])

    refused = client.post(f"/api/strategies/{ml_id}/versions/2/swap", headers=AUTH)
    assert refused.status_code == 409
    assert {c["name"] for c in refused.json()["failing_checks"]} >= {"min_days"}
    short = client.post(
        f"/api/strategies/{ml_id}/versions/2/swap",
        json={"override": True, "reason": "short"},
        headers=AUTH,
    )
    assert short.status_code == 422

    swapped = client.post(
        f"/api/strategies/{ml_id}/versions/2/swap",
        json={"override": True, "reason": LONG_REASON},
        headers=AUTH,
    )
    assert swapped.status_code == 200, swapped.text
    assert swapped.json()["status"] == "live"

    history = client.get(f"/api/strategies/{ml_id}/versions/history", headers=AUTH).json()
    kinds = [(e["version"], e["kind"], e["to_status"]) for e in history["items"]]
    assert kinds == [
        (1, "baseline", "live"),
        (2, "candidate", "candidate"),
        (1, "swap", "archived"),
        (2, "swap", "live"),
    ]
    assert history["items"][-1]["override"] is True


def test_reject_and_unknown_ids(client, ml_id):
    _retrain(client, ml_id)
    no_reason = client.post(f"/api/strategies/{ml_id}/versions/2/reject", headers=AUTH)
    assert no_reason.status_code == 422
    ok = client.post(
        f"/api/strategies/{ml_id}/versions/2/reject", json={"reason": "worse fit"}, headers=AUTH
    )
    assert ok.json()["status"] == "rejected"
    assert client.get("/api/strategies/nope/versions", headers=AUTH).status_code == 404
    assert client.get(f"/api/strategies/{ml_id}/versions/9/check", headers=AUTH).status_code == 404
    missing = client.get(f"/api/strategies/{ml_id}/versions/9/calibration", headers=AUTH)
    assert missing.status_code == 404


def test_writes_need_permission(remote, ml_id):
    assert remote.post(f"/api/strategies/{ml_id}/versions/2/swap").status_code == 401
    assert remote.post("/api/model-versions/retrain").status_code == 401

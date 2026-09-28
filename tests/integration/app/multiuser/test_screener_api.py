"""Screener routes (roadmap 20.8): metrics, running a screen, saved screens
that stay with their owner, and a screen stored as a universe."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from stonks.store.lake import DuckDBLake
from tests.fixtures.screener import END, seed_market

AS_OF = END.isoformat()


@pytest.fixture
def market(settings, seeded):
    with DuckDBLake(settings.lake.path) as lake:
        seed_market(lake)


def test_metrics_list(client, people):
    res = client.get("/api/screener/metrics", headers=people["vic"]["headers"])
    assert res.status_code == 200
    by_id = {m["id"]: m for m in res.json()}
    assert by_id["pe_ratio"]["group"] == "fundamental"
    assert by_id["return_12m"]["unit"] == "percent"


def test_run_a_screen(client, people, market):
    body = {
        "spec": {"sectors": ["Tech"], "filters": [{"metric": "return_12m", "min": 0.5}]},
        "as_of": AS_OF,
    }
    res = client.post("/api/screener/run", json=body, headers=people["vic"]["headers"])
    assert res.status_code == 200, res.text
    out = res.json()
    assert [r["ticker"] for r in out["rows"]] == ["AAA.US"]
    assert out["candidates"] == 2 and out["metrics"] == ["return_12m"]
    bad = client.post(
        "/api/screener/run",
        json={"spec": {"filters": [{"metric": "nope", "min": 1}]}},
        headers=people["vic"]["headers"],
    )
    assert bad.status_code == 422
    both = client.post(
        "/api/screener/run", json={"spec": {}, "screen_id": "x"}, headers=people["vic"]["headers"]
    )
    assert both.status_code == 422
    ghost = client.post(
        "/api/screener/run",
        json={"spec": {"universe_id": "ghost"}, "as_of": AS_OF},
        headers=people["vic"]["headers"],
    )
    assert ghost.status_code == 404


def test_saved_screens_stay_with_their_owner(client, people, market):
    alice, bob, vic = (people[n]["headers"] for n in ("alice", "bob", "vic"))
    spec = {"sort_by": "price", "limit": 1}
    made = client.post("/api/screener/screens", json={"name": "Top", "spec": spec}, headers=alice)
    assert made.status_code == 201, made.text
    sid = made.json()["id"]
    assert made.json()["spec"]["limit"] == 1
    clash = client.post("/api/screener/screens", json={"name": "Top", "spec": {}}, headers=alice)
    assert clash.status_code == 409
    assert (
        client.post(
            "/api/screener/screens", json={"name": "V", "spec": {}}, headers=vic
        ).status_code
        == 403
    )
    assert client.get(f"/api/screener/screens/{sid}", headers=bob).status_code == 404
    assert client.get("/api/screener/screens", headers=bob).json()["items"] == []
    run = client.post(
        "/api/screener/run", json={"screen_id": sid, "as_of": AS_OF}, headers=alice
    ).json()
    assert [r["ticker"] for r in run["rows"]] == ["BBB.US"]
    assert client.post("/api/screener/run", json={"screen_id": sid}, headers=bob).status_code == 404
    renamed = client.patch(
        f"/api/screener/screens/{sid}", json={"name": "Best", "spec": {"limit": 2}}, headers=alice
    )
    assert renamed.status_code == 200
    assert renamed.json()["name"] == "Best" and renamed.json()["spec"]["limit"] == 2
    assert (
        client.patch(f"/api/screener/screens/{sid}", json={"name": "x"}, headers=bob).status_code
        == 404
    )
    assert client.delete(f"/api/screener/screens/{sid}", headers=bob).status_code == 404
    assert client.delete(f"/api/screener/screens/{sid}", headers=alice).status_code == 204
    assert client.get(f"/api/screener/screens/{sid}", headers=alice).status_code == 404


def test_save_a_screen_as_a_rule_universe(client, people, market):
    alice = people["alice"]["headers"]
    sid = client.post(
        "/api/screener/screens",
        json={"name": "Rising", "spec": {"filters": [{"metric": "return_3m", "min": 0.01}]}},
        headers=alice,
    ).json()["id"]
    res = client.post(
        "/api/screener/universes",
        json={
            "universe_id": "rising",
            "screen_id": sid,
            "start": "2024-06-03",
            "end": AS_OF,
        },
        headers=alice,
    )
    assert res.status_code == 201, res.text
    out = res.json()
    assert out["universe"]["kind"] == "rule"
    assert out["universe"]["spec"]["filters"][0]["metric"] == "return_3m"
    assert out["universe"]["description"] == "screen 'Rising'"
    job = client.app.state.services.runner.wait(out["refresh_job"]["id"], timeout=30)
    assert job.status == "succeeded", job.error
    members = client.get(
        "/api/universes/rising/members", params={"as_of": AS_OF}, headers=alice
    ).json()
    assert members["tickers"] == ["AAA.US"]
    again = client.post(
        "/api/screener/universes",
        json={"universe_id": "rising", "spec": {}, "refresh": False},
        headers=alice,
    )
    assert again.status_code == 409
    loop = client.post(
        "/api/screener/universes",
        json={"universe_id": "self", "spec": {"universe_id": "self"}},
        headers=alice,
    )
    assert loop.status_code == 422
    viewer = client.post(
        "/api/screener/universes",
        json={"universe_id": "v", "spec": {}},
        headers=people["vic"]["headers"],
    )
    assert viewer.status_code == 403


def test_save_a_snapshot_warns_about_survivorship(client, people, market):
    alice = people["alice"]["headers"]
    services = client.app.state.services
    services.screener._clock = lambda: datetime(2024, 12, 31, 12, tzinfo=UTC)
    res = client.post(
        "/api/screener/universes",
        json={
            "universe_id": "tech_now",
            "spec": {"sectors": ["Tech"]},
            "mode": "snapshot",
            "refresh": False,
        },
        headers=alice,
    )
    assert res.status_code == 201, res.text
    out = res.json()
    assert out["universe"]["kind"] == "list"
    assert out["universe"]["spec"]["tickers"] == ["AAA.US", "BBB.US"]
    assert out["refresh_job"] is None
    assert "survivorship" in out["warnings"][0]
    empty = client.post(
        "/api/screener/universes",
        json={"universe_id": "none", "spec": {"sectors": ["Nope"]}, "mode": "snapshot"},
        headers=alice,
    )
    assert empty.status_code == 422
    dated = client.post(
        "/api/screener/universes",
        json={"universe_id": "d", "spec": {}, "mode": "snapshot", "start": "2024-01-01"},
        headers=alice,
    )
    assert dated.status_code == 422


# ---- screens at scale (roadmap 20.11) ------------------------------------------------------


def test_size_says_when_to_use_the_job(client, people, market, settings):
    vic = people["vic"]["headers"]
    body = {"spec": {"sectors": ["Tech"]}, "as_of": AS_OF}
    res = client.post("/api/screener/size", json=body, headers=vic)
    assert res.status_code == 200, res.text
    out = res.json()
    assert out["candidates"] == 2 and out["as_of"] == AS_OF
    assert out["max_candidates"] == settings.screener.max_candidates
    assert out["over_cap"] is False and out["use_job"] is False
    settings.screener.job_threshold = 1
    assert client.post("/api/screener/size", json=body, headers=vic).json()["use_job"] is True
    settings.screener.max_candidates = 1
    over = client.post("/api/screener/size", json=body, headers=vic).json()
    assert over["over_cap"] is True and over["use_job"] is False


def test_the_cap_is_a_clear_422(client, people, market, settings):
    settings.screener.max_candidates = 2
    res = client.post(
        "/api/screener/run",
        json={"spec": {"sectors": ["Tech", "Energy"]}, "as_of": AS_OF},
        headers=people["vic"]["headers"],
    )
    assert res.status_code == 422
    assert "3 candidates" in res.text and "cap of 2" in res.text


def test_a_screen_as_a_background_job(client, people, market):
    alice, bob = people["alice"]["headers"], people["bob"]["headers"]
    body = {
        "spec": {"sectors": ["Tech", "Energy"], "sort_by": "price", "columns": ["return_12m"]},
        "as_of": AS_OF,
    }
    res = client.post("/api/screener/jobs", json=body, headers=alice)
    assert res.status_code == 202, res.text
    job_id = res.json()["id"]
    assert res.json()["kind"] == "screen_run"
    job = client.app.state.services.runner.wait(job_id, timeout=30)
    assert job.status == "succeeded", job.error
    assert job.progress == 1.0
    out = client.get(f"/api/screener/jobs/{job_id}/result", headers=alice)
    assert out.status_code == 200, out.text
    assert [r["ticker"] for r in out.json()["rows"]] == ["BBB.US", "AAA.US", "CCC.US"]
    assert out.json()["metrics"] == ["price", "return_12m"]
    # another person's job is missing
    assert client.get(f"/api/screener/jobs/{job_id}/result", headers=bob).status_code == 404
    # a saved screen by id, and someone else's saved screen is a 404 at submit
    sid = client.post(
        "/api/screener/screens", json={"name": "Top", "spec": {"limit": 1}}, headers=alice
    ).json()["id"]
    queued = client.post("/api/screener/jobs", json={"screen_id": sid}, headers=alice)
    assert queued.status_code == 202
    assert (
        client.post("/api/screener/jobs", json={"screen_id": sid}, headers=bob).status_code == 404
    )


def test_a_job_over_the_cap_fails_with_the_message(client, people, market, settings):
    settings.screener.max_candidates = 1
    alice = people["alice"]["headers"]
    res = client.post("/api/screener/jobs", json={"spec": {}, "as_of": AS_OF}, headers=alice)
    job = client.app.state.services.runner.wait(res.json()["id"], timeout=30)
    assert job.status == "failed" and "cap of 1" in (job.error or "")
    result = client.get(f"/api/screener/jobs/{job.id}/result", headers=alice)
    assert result.status_code == 409


def test_a_repeat_run_comes_from_the_cache(client, people, market):
    vic = people["vic"]["headers"]
    body = {"spec": {"sort_by": "price"}, "as_of": AS_OF}
    first = client.post("/api/screener/run", json=body, headers=vic).json()
    assert first["cached"] is False
    again = client.post("/api/screener/run", json=body, headers=vic).json()
    assert again["cached"] is True and again["rows"] == first["rows"]
    other_day = client.post(
        "/api/screener/run", json={**body, "as_of": "2024-12-30"}, headers=vic
    ).json()
    assert other_day["cached"] is False

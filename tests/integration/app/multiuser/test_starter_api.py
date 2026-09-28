"""The starter set over HTTP (complexity audit F19): an admin installs it,
the strategies show up On trial with a starter label, the first-run system
check sees them, and nothing is approved."""

from __future__ import annotations

from stonks.starter import STARTER_UNIVERSE, STARTERS

IDS = {s.id for s in STARTERS}


def test_an_admin_installs_the_starter_set_on_trial(client, people):
    ada, alice = people["ada"]["headers"], people["alice"]["headers"]
    before = client.get("/api/starter", headers=alice).json()
    assert before["installed"] is False and before["universe"] == list(STARTER_UNIVERSE)
    assert client.post("/api/starter/install", headers=alice).status_code == 403

    done = client.post("/api/starter/install", headers=ada)
    assert done.status_code == 200, done.text
    body = done.json()
    assert set(body["registered"]) == IDS and body["skipped"] == []
    assert body["universe"] == list(STARTER_UNIVERSE) and body["next_steps"]

    strategies = client.get("/api/strategies", headers=alice).json()
    items = strategies["items"] if isinstance(strategies, dict) else strategies
    starters = {s["id"]: s for s in items if s["id"] in IDS}
    assert set(starters) == IDS
    for s in starters.values():
        assert s["status"] == "shadow"  # On trial, never approved
        assert s["starter"]["title"].startswith("Starter:")
    assert all(s["starter"] is None for s in items if s["id"] not in IDS)

    after = client.get("/api/starter", headers=alice).json()
    assert after["installed"] is True
    assert {s["status"] for s in after["strategies"]} == {"shadow"}
    again = client.post("/api/starter/install", headers=ada).json()
    assert again["registered"] == [] and set(again["skipped"]) == IDS
    assert again["universe"] is None


def test_the_admin_checklist_counts_strategies(client, people):
    ada = people["ada"]["headers"]
    checks = {
        c["id"]: c for c in client.get("/api/onboarding/system", headers=ada).json()["checks"]
    }
    assert checks["strategies"]["done"] is True  # the seeded workspace has strategies
    assert (
        "on trial" in checks["strategies"]["detail"] or "approved" in checks["strategies"]["detail"]
    )

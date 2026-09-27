"""The trial ledger read back (roadmap 18.7, BL-04): recorded lab runs with
their hypothesis and trials, for anyone who may run the lab."""

from __future__ import annotations

import math

from stonks.lab.trials import LabRunSpec, TrialLedger, TrialRecord
from stonks.store.state import SqliteState

CLASS = "stonks.strategies.examples.momentum:Momentum"


def _record(settings, hypothesis: str, scores: list[float], verdict="fail") -> str:
    with SqliteState(settings.state.path) as state:
        ledger = TrialLedger(state, settings.registry.artifacts_dir)
        spec = LabRunSpec(
            strategy_class=CLASS,
            hypothesis=hypothesis,
            premortem="it stops working when trends vanish",
            tuner="random",
            objective="sharpe",
            budget=len(scores),
            seed=0,
            dataset={
                "universe": ["UP.US", "DOWN.US"],
                "full_window": ["2025-10-01", "2026-04-01"],
                "interval": "1d",
                "universe_id": "mine",
            },
        )
        trials = [
            TrialRecord(trial_index=i, params={"lookback_days": 5 + i}, score=s)
            for i, s in enumerate(scores)
        ]
        return ledger.record_run(spec, trials, verdict=verdict)


def test_the_ledger_lists_runs_newest_first_with_trial_counts(client, settings, people):
    first = _record(settings, "trend persists", [0.4, math.nan])
    second = _record(settings, "trend persists again", [1.2], verdict="pass")
    alice = people["alice"]["headers"]
    page = client.get("/api/lab/ledger", headers=alice)
    assert page.status_code == 200, page.text
    body = page.json()
    assert [r["id"] for r in body["items"]] == [second, first] and body["total"] == 2
    old = body["items"][1]
    assert (old["n_trials"], old["n_failed"], old["best_score"]) == (2, 1, 0.4)
    assert old["hypothesis"] == "trend persists" and old["verdict"] == "fail"
    assert (old["universe_id"], old["tickers"], old["start"], old["end"]) == (
        "mine",
        2,
        "2025-10-01",
        "2026-04-01",
    )
    filtered = client.get("/api/lab/ledger", params={"strategy_class": "x:Y"}, headers=alice)
    assert filtered.json()["total"] == 0


def test_one_run_shows_every_trial_and_the_class_count(client, settings, people):
    run_id = _record(settings, "trend persists", [0.4, math.nan])
    _record(settings, "another try", [0.1])
    detail = client.get(f"/api/lab/ledger/{run_id}", headers=people["bob"]["headers"])
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert [(t["trial_index"], t["score"], t["status"]) for t in body["trials"]] == [
        (0, 0.4, "ok"),
        (1, None, "failed"),
    ]
    assert body["n_trials_class"] == 3
    missing = client.get("/api/lab/ledger/lab_missing", headers=people["bob"]["headers"])
    assert missing.status_code == 404


def test_viewers_cannot_read_the_ledger(client, people):
    assert client.get("/api/lab/ledger", headers=people["vic"]["headers"]).status_code == 403

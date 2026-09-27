"""Options research over the API (roadmap 17.6): underlyings with stored
chains, a chain with Greeks, the strategies and structures, a payoff, and
an options backtest job with its validation. Synthetic chains (hermetic)."""

from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient

from stonks.api import create_app
from stonks.options.ingest import ingest_option_quotes
from stonks.options.synthetic import SyntheticChainSpec, SyntheticOptionSource
from stonks.store.lake import DuckDBLake
from tests.integration.app.test_api import AUTH, LOOPBACK, _wait_job

FIRST, LAST = date(2026, 1, 2), date(2026, 3, 31)


def seed_option_lake(path) -> None:
    """Synthetic chains for UP.US from its stored closes (the seeded lake)."""
    with DuckDBLake(path) as lake:
        lake.migrate()
        frame = lake.get_prices("UP.US", FIRST, LAST)
        closes = {d: float(c) for d, c in zip(frame["date"], frame["close"], strict=True)}
        src = SyntheticOptionSource(
            {"UP.US": closes},
            SyntheticChainSpec(horizon_days=70),
            fixed_strikes={"UP.US": [float(k) for k in range(120, 225, 5)]},
        )
        ingest_option_quotes(src, lake, ["UP.US"], since=FIRST, until=LAST)


@pytest.fixture
def client(settings, seeded, fake_source):
    settings.api.allowed_hosts = ["testserver"]
    seed_option_lake(settings.lake.path)
    app = create_app(settings, source_factory=lambda: fake_source)
    with TestClient(app, client=LOOPBACK) as c:
        yield c


def test_underlyings_and_a_chain_with_greeks(client):
    assert client.get("/api/options/underlyings").status_code == 401
    listed = client.get("/api/options/underlyings", headers=AUTH).json()
    assert [u["underlying"] for u in listed] == ["UP.US"]
    up = listed[0]
    assert up["first_day"] == "2026-01-02" and up["last_day"] == "2026-03-31"
    assert up["synthetic"] is True and up["sources"] == ["synthetic"]

    chain = client.get(
        "/api/options/chains/UP.US", params={"as_of": "2026-02-01"}, headers=AUTH
    ).json()
    # a Sunday: the last stored day before it
    assert chain["as_of"] == "2026-01-30"
    assert chain["expiry"] in chain["expiries"] and 0 < chain["days_to_expiry"] <= 60
    assert chain["synthetic"] is True and chain["models"] == ["american_baw"]
    strikes = [r["strike"] for r in chain["rows"]]
    assert strikes == sorted(strikes) and len(strikes) == 21
    atm = min(chain["rows"], key=lambda r: abs(r["strike"] - chain["spot"]))
    call, put = atm["call"], atm["put"]
    assert 0.3 < call["delta"] < 0.7 and -0.7 < put["delta"] < -0.3
    assert call["gamma"] > 0 and call["vega"] > 0 and call["theta"] < 0
    assert call["iv"] == pytest.approx(0.25, abs=0.01)
    assert call["bid"] < call["mark"] < call["ask"]

    other = chain["expiries"][-1]
    picked = client.get(
        "/api/options/chains/UP.US", params={"as_of": "2026-01-30", "expiry": other}, headers=AUTH
    ).json()
    assert picked["expiry"] == other and picked["rows"][0]["call"]["contract_id"].endswith(
        ":C:120"
    )
    latest = client.get("/api/options/chains/UP.US", headers=AUTH).json()
    assert latest["as_of"] == "2026-03-31"

    missing = client.get("/api/options/chains/NOPE.US", headers=AUTH)
    assert missing.status_code == 404
    early = client.get("/api/options/chains/UP.US", params={"as_of": "2025-01-01"}, headers=AUTH)
    assert early.status_code == 404
    bad_expiry = client.get(
        "/api/options/chains/UP.US", params={"expiry": "2030-01-18"}, headers=AUTH
    )
    assert bad_expiry.status_code == 404


def test_strategies_structures_and_payoff(client):
    strategies = client.get("/api/options/strategies", headers=AUTH).json()
    ids = {s["id"] for s in strategies}
    assert {"covered_call", "cash_secured_put", "vertical_spread"} <= ids
    csp = next(s for s in strategies if s["id"] == "cash_secured_put")
    assert csp["hypothesis"] and csp["structures"] and csp["parameters"]

    structures = {s["name"]: s for s in client.get("/api/options/structures", headers=AUTH).json()}
    assert "close_group" not in structures and structures["covered_call"]["holds_shares"]

    body = {"underlying": "UP.US", "as_of": "2026-02-02", "structure": "bull_call_spread"}
    payoff = client.post("/api/options/payoff", json=body, headers=AUTH)
    assert payoff.status_code == 200, payoff.text
    p = payoff.json()
    assert p["as_of"] == "2026-02-02" and len(p["legs"]) == 2
    assert [leg["quantity"] for leg in p["legs"]] == [1, -1]
    assert p["cost"] > 0 and p["max_loss"] == pytest.approx(p["cost"])
    assert len(p["breakevens"]) == 1 and len(p["points"]) > 50

    naked = client.post(
        "/api/options/payoff", json=body | {"structure": "long_call", "delta": 0.4}, headers=AUTH
    ).json()
    assert naked["max_gain"] is None and naked["max_loss"] > 0

    unknown = client.post(
        "/api/options/payoff", json=body | {"structure": "close_group"}, headers=AUTH
    )
    assert unknown.status_code == 422
    bad_delta = client.post("/api/options/payoff", json=body | {"delta": 1.5}, headers=AUTH)
    assert bad_delta.status_code == 422


def test_backtest_job_with_validation(client):
    body = {
        "strategy": "cash_secured_put",
        "underlyings": ["UP.US"],
        "start": "2026-01-02",
        "end": "2026-03-31",
    }
    assert client.post("/api/options/backtests", json=body).status_code == 401
    resp = client.post("/api/options/backtests", json=body, headers=AUTH)
    assert resp.status_code == 202, resp.text
    job = resp.json()
    assert job["kind"] == "options_backtest"
    assert _wait_job(client, job["id"])["status"] == "succeeded"
    result = client.get(f"/api/options/backtests/{job['id']}/result", headers=AUTH)
    assert result.status_code == 200, result.text
    r = result.json()
    assert r["strategy"] == "cash_secured_put" and r["days"] == len(r["equity"]) > 50
    assert r["fills"] > 0 and r["synthetic"] is True
    assert {v["test_id"] for v in r["validation"]} == {
        "oos",
        "deflated_sharpe",
        "fill_stress",
        "missing_quotes",
        "cost_stress",
    }
    assert r["verdict"] in ("passed", "failed")
    assert r["verdict"] == ("passed" if all(v["passed"] for v in r["validation"]) else "failed")


def test_backtest_requests_are_checked_before_queueing(client):
    body = {
        "strategy": "vertical_spread",
        "underlyings": ["UP.US"],
        "start": "2026-01-02",
        "end": "2026-02-27",
        "validation": False,
    }
    unknown = client.post(
        "/api/options/backtests", json=body | {"strategy": "nope"}, headers=AUTH
    )
    assert unknown.status_code == 422
    bad_param = client.post(
        "/api/options/backtests", json=body | {"params": {"lookback": 5000}}, headers=AUTH
    )
    assert bad_param.status_code == 422
    no_chains = client.post(
        "/api/options/backtests", json=body | {"underlyings": ["FLAT.US"]}, headers=AUTH
    )
    assert no_chains.status_code == 422 and "stonks options ingest" in no_chains.text
    inverted = client.post(
        "/api/options/backtests", json=body | {"start": "2026-03-01"}, headers=AUTH
    )
    assert inverted.status_code == 422

    job = client.post("/api/options/backtests", json=body, headers=AUTH).json()
    assert _wait_job(client, job["id"])["status"] == "succeeded"
    r = client.get(f"/api/options/backtests/{job['id']}/result", headers=AUTH).json()
    assert r["verdict"] == "not_run" and r["validation"] == []

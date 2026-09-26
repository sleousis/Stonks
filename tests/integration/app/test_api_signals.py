"""Signal research over the API: ``POST /api/lab/signal-ic`` queues a job;
``GET /api/lab/signal-ic/{job_id}/result`` returns the typed SignalICResult."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from stonks.api import create_app
from stonks.config import LabSettings
from stonks.lab.parallel import ParallelSettings
from stonks.store.lake import DuckDBLake
from tests.integration.app.test_api import AUTH, LOOPBACK, _wait_job

MOMENTUM = "stonks.strategies.examples.momentum:Momentum"
TICKERS = [f"SIG{i:02d}.US" for i in range(12)]


def _seed_walks(path) -> None:
    rng = np.random.default_rng(7)
    dates = pd.bdate_range(start="2025-10-01", end="2026-04-01")
    rows = []
    for ticker in TICKERS:
        closes = 50.0 * np.exp(np.cumsum(rng.normal(0.0005, 0.02, len(dates))))
        for d, c in zip(dates, closes, strict=True):
            rows.append(
                {"ticker": ticker, "date": d.date(), "open": c, "high": c * 1.01,
                 "low": c * 0.99, "close": c, "adj_close": c, "volume": 1_000_000}
            )  # fmt: skip
    lake = DuckDBLake(path)
    try:
        lake.upsert_prices(pd.DataFrame(rows))
    finally:
        lake.close()


@pytest.fixture
def client(settings, seeded, fake_source):
    settings.api.allowed_hosts = ["testserver"]
    settings.lab = LabSettings(parallel=ParallelSettings(max_workers=1))
    _seed_walks(settings.lake.path)
    app = create_app(settings, source_factory=lambda: fake_source)
    with TestClient(app, client=LOOPBACK) as c:
        yield c


def _body(**over) -> dict:
    body = {
        "strategy": {"class_path": MOMENTUM, "params": {"lookback_days": 10, "threshold": -1.0}},
        "universe": TICKERS,
        "start": "2025-10-01",
        "end": "2026-04-01",
        "horizons": [1, 5],
        "every_bars": 5,
    }
    return body | over


def test_signal_ic_job_and_typed_result(client):
    resp = client.post("/api/lab/signal-ic", json=_body(), headers=AUTH)
    assert resp.status_code == 202, resp.text
    job = resp.json()
    assert job["kind"] == "signal_ic"
    assert resp.headers["location"] == f"/api/jobs/{job['id']}"
    assert _wait_job(client, job["id"])["status"] == "succeeded"

    result = client.get(f"/api/lab/signal-ic/{job['id']}/result")
    assert result.status_code == 200, result.text
    body = result.json()
    assert body["status"] == "ok"
    assert body["n_tickers"] == len(TICKERS)
    assert body["window"] == ["2025-10-01", "2026-04-01"]
    assert [h["horizon"] for h in body["horizons"]] == [1, 5]
    assert body["ic_horizon"] == 5
    assert math.isfinite(body["ic_estimate"])
    assert len(body["horizons"][0]["quantile_means"]) == 5


def test_small_universe_is_not_applicable(client):
    resp = client.post("/api/lab/signal-ic", json=_body(universe=TICKERS[:3]), headers=AUTH)
    job = resp.json()
    assert _wait_job(client, job["id"])["status"] == "succeeded"
    body = client.get(f"/api/lab/signal-ic/{job['id']}/result").json()
    assert body["status"] == "n/a"
    assert body["ic_estimate"] is None
    assert "at least" in body["note"]


def test_signal_ic_validation(client):
    assert client.post("/api/lab/signal-ic", json=_body()).status_code == 401
    bad = [
        _body(start="2026-04-01", end="2025-10-01"),
        _body(horizons=[]),
        _body(horizons=[0]),
        _body(every_bars=0),
        _body(interval="7x"),
        _body(strategy={"class_path": "nope:Nope"}),
    ]
    for body in bad:
        resp = client.post("/api/lab/signal-ic", json=body, headers=AUTH)
        assert resp.status_code == 422, (body, resp.text)


def test_result_of_another_kind_is_404(client):
    job = client.post(
        "/api/lab/backtests",
        json={
            "strategy": {"class_path": MOMENTUM, "params": {}},
            "universe": ["UP.US"],
            "start": "2025-10-01",
            "end": "2026-04-01",
        },
        headers=AUTH,
    ).json()
    assert client.get(f"/api/lab/signal-ic/{job['id']}/result").status_code == 404

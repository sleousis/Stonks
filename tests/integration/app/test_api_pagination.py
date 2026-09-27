"""AS-17: every list route answers one shape, ``{items, total, limit, offset}``,
and takes ``limit`` and ``offset``. Only fixed catalogs (a handful of
entries that never grow with use) stay plain lists."""

from __future__ import annotations

import json

import pytest

from stonks.api.openapi import render_openapi
from tests.integration.app.test_api import AUTH

#: Reference lists whose size is set by the code, not by use.
CATALOGS = {
    "/api/catalog/strategies",
    "/api/catalog/intervals",
    "/api/catalog/asset-classes",
    "/api/connections/providers",
    "/api/lab/survival-tests",
    "/api/lab/survival-presets",
    "/api/lab/cost-models",
    "/api/sources",
    "/api/studio/templates",
}

#: Record lists that were plain lists before AS-17 (a route with no id in
#: its path, readable by the default admin with no setup).
PAGED = [
    "/api/auth/tokens",
    "/api/auth/users",
    "/api/backups",
    "/api/connections",
    "/api/halts",
    "/api/push/subscriptions",
    "/api/portfolios",
    "/api/portfolios/trading-modes",
    "/api/subscriptions",
    "/api/universes",
]


def _list_routes() -> dict[str, dict]:
    spec = json.loads(render_openapi())
    out = {}
    for path, ops in spec["paths"].items():
        get = ops.get("get")
        if not get:
            continue
        schema = get["responses"].get("200", {}).get("content", {})
        schema = schema.get("application/json", {}).get("schema", {})
        out[path] = {"schema": schema, "params": {p["name"] for p in get.get("parameters", [])}}
    return out


def test_only_catalogs_answer_a_plain_list():
    plain = {p for p, r in _list_routes().items() if r["schema"].get("type") == "array"}
    assert plain == CATALOGS


def test_every_paged_route_takes_limit_and_offset():
    spec = json.loads(render_openapi())
    for path, route in _list_routes().items():
        ref = route["schema"].get("$ref", "")
        if not ref.split("/")[-1].startswith("Page_"):
            continue
        props = spec["components"]["schemas"][ref.split("/")[-1]]["properties"]
        assert set(props) >= {"items", "total", "limit", "offset"}, path
        assert {"limit", "offset"} <= route["params"], path


@pytest.mark.parametrize("path", PAGED)
def test_record_lists_answer_a_page(client, path):
    resp = client.get(path, headers=AUTH, params={"limit": 1, "offset": 0})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {"items", "total", "limit", "offset"}
    assert body["limit"] == 1 and body["offset"] == 0
    assert len(body["items"]) <= 1 and body["total"] >= len(body["items"])


def test_strategy_history_answers_a_page(client, seeded):
    sid = next(iter(client.get("/api/strategies", headers=AUTH).json()["items"]))["id"]
    body = client.get(f"/api/strategies/{sid}/history", headers=AUTH).json()
    assert set(body) == {"items", "total", "limit", "offset"}


def test_a_page_slices_and_counts_the_whole_list(client):
    for name in ("one", "two", "three"):
        created = client.post(
            "/api/universes",
            json={"id": f"pg-{name}", "kind": "list", "spec": {"tickers": ["AAPL.US"]}},
            headers=AUTH,
        )
        assert created.status_code in (200, 201), created.text
    first = client.get("/api/universes", headers=AUTH, params={"limit": 2}).json()
    rest = client.get("/api/universes", headers=AUTH, params={"limit": 2, "offset": 2}).json()
    assert first["total"] == rest["total"] >= 3
    ids = [u["id"] for u in first["items"] + rest["items"]]
    assert len(ids) == len(set(ids)) and {"pg-one", "pg-two", "pg-three"} <= set(ids)

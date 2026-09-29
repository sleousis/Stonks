"""REST routes the trader console needs: token check, cost basis, typed tick
runs, strategy counts and the alerts feed."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from stonks.api import create_app
from stonks.notify import Notification, StoreNotifier
from stonks.store.state import SqliteState
from tests.integration.app.test_api import AUTH, LOOPBACK

# ---- auth check ---------------------------------------------------------------


def test_auth_check_accepts_valid_token(client, remote):
    for c in (client, remote):
        resp = c.get("/api/auth/check", headers=AUTH)
        assert resp.status_code == 200
        assert resp.json() == {"authenticated": True}


def test_auth_check_rejects_missing_or_bad_token_even_on_loopback(client, remote):
    for c in (client, remote):
        for headers in ({}, {"Authorization": "Bearer nope"}):
            resp = c.get("/api/auth/check", headers=headers)
            assert resp.status_code == 401
            assert resp.headers["content-type"].startswith("application/problem+json")


def test_auth_check_without_configured_token_is_503(settings, seeded, monkeypatch):
    monkeypatch.delenv("STONKS_API_TOKEN")
    settings.api = settings.api.model_copy(update={"token": None, "allowed_hosts": ["testserver"]})
    with TestClient(create_app(settings), client=LOOPBACK) as c:
        assert c.get("/api/auth/check", headers=AUTH).status_code == 503


# ---- portfolio cost basis -----------------------------------------------------


def test_positions_carry_cost_basis_and_unrealized_pnl(client, settings, seeded):
    with SqliteState(settings.state.path) as state:
        [fill] = state.sql("SELECT quantity, price, fee FROM fills")
    avg = (fill["quantity"] * fill["price"] + fill["fee"]) / fill["quantity"]

    body = client.get("/api/portfolio", headers=AUTH).json()
    assert body["currency"] == "USD"
    [pos] = body["positions"]
    assert pos["ticker"] == "UP.US"
    assert pos["avg_cost"] == pytest.approx(avg)
    assert pos["cost_basis"] == pytest.approx(avg * pos["quantity"])
    expected_pnl = (pos["price"] - avg) * pos["quantity"]
    assert pos["unrealized_pnl"] == pytest.approx(expected_pnl)
    assert pos["unrealized_pnl_pct"] == pytest.approx(pos["price"] / avg - 1)
    assert body["unrealized_pnl"] == pytest.approx(expected_pnl)
    assert body["cost_basis"] == pytest.approx(pos["cost_basis"])


def test_average_cost_follows_a_split_the_book_went_through(client, settings, seeded):
    """After a 2:1 split the book holds twice the shares at half the cost
    each. The average cost replayed from the fills must say so, or the
    position shows a loss of half its value."""
    with SqliteState(settings.state.path) as state:
        [fill] = state.sql("SELECT quantity, price, fee FROM fills")
        [snap] = state.sql(
            "SELECT cash, positions_json FROM portfolio_snapshots ORDER BY id DESC LIMIT 1"
        )
        held = float(fill["quantity"])
        state.execute(
            "INSERT INTO corporate_action_ledger (portfolio_id, ticker, ex_date, kind, value,"
            " quantity_before, quantity_after, cash_delta, applied_at) VALUES"
            " ('pf_default', 'UP.US', '2026-03-25', 'split', 2.0, ?, ?, 0, '2026-03-25')",
            [held, held * 2],
        )
        state.execute(
            "INSERT INTO portfolio_snapshots (taken_at, as_of, cash, positions_json, total_value,"
            " portfolio_id, source) VALUES ('2026-03-25T21:00:00+00:00', '2026-03-25', ?, ?,"
            " 0, 'pf_default', 'tick')",
            [snap["cash"], json.dumps({"UP.US": held * 2})],
        )
    avg = (fill["quantity"] * fill["price"] + fill["fee"]) / fill["quantity"]
    [pos] = client.get("/api/portfolio", headers=AUTH).json()["positions"]
    assert pos["quantity"] == pytest.approx(held * 2)
    assert pos["avg_cost"] == pytest.approx(avg / 2)
    assert pos["cost_basis"] == pytest.approx(avg * held)


_OPTION = "UP.US:2026-06-19:C:10"


def _hold_an_option(settings, *, contracts: float = 2.0, premium: float = 1.5) -> None:
    """A filled buy of ``contracts`` calls at ``premium`` per share, and a
    snapshot that holds them beside the seeded stock."""
    with SqliteState(settings.state.path) as state:
        [snap] = state.sql(
            "SELECT cash, positions_json FROM portfolio_snapshots ORDER BY id DESC LIMIT 1"
        )
        state.execute(
            "INSERT INTO orders (client_id, ticker, side, quantity, order_type, status,"
            " created_at, updated_at, portfolio_id, origin) VALUES ('opt-1', ?, 'buy', ?,"
            " 'market', 'filled', '2026-03-21', '2026-03-21', 'pf_default', 'manual')",
            [_OPTION, contracts],
        )
        state.execute(
            "INSERT INTO fills (order_client_id, ticker, quantity, price, fee, filled_at,"
            " portfolio_id) VALUES ('opt-1', ?, ?, ?, 0, '2026-03-21T15:00:00+00:00',"
            " 'pf_default')",
            [_OPTION, contracts, premium],
        )
        held = {**json.loads(snap["positions_json"]), _OPTION: contracts}
        state.execute(
            "INSERT INTO portfolio_snapshots (taken_at, as_of, cash, positions_json, total_value,"
            " portfolio_id, source) VALUES ('2026-03-23T21:00:00+00:00', '2026-03-23', ?, ?,"
            " 0, 'pf_default', 'tick')",
            [float(snap["cash"]) - contracts * premium * 100, json.dumps(held)],
        )


def test_an_option_position_is_priced_from_its_quotes_with_the_multiplier(client, settings, seeded):
    from datetime import date

    from stonks.ingest.option_schemas import OptionQuoteRow
    from stonks.options.store import OptionStore
    from stonks.store.lake import DuckDBLake

    _hold_an_option(settings)
    with DuckDBLake(settings.lake.path) as lake:
        OptionStore(lake).upsert_quotes(
            [
                OptionQuoteRow(
                    underlying="UP.US",
                    expiry=date(2026, 6, 19),
                    strike=10.0,
                    right="call",
                    as_of=date(2026, 3, 23),
                    bid=1.9,
                    ask=2.1,
                )
            ],
            source="test",
        )
    body = client.get("/api/portfolio", headers=AUTH).json()
    opt = next(p for p in body["positions"] if p["ticker"] == _OPTION)
    stock = next(p for p in body["positions"] if p["ticker"] == "UP.US")
    assert opt["multiplier"] == 100
    assert opt["price"] == pytest.approx(2.0)  # the mid, per share
    assert opt["price_date"] == "2026-03-23"
    assert opt["market_value"] == pytest.approx(2 * 2.0 * 100)
    assert opt["avg_cost"] == pytest.approx(1.5)
    assert opt["cost_basis"] == pytest.approx(2 * 1.5 * 100)
    assert opt["unrealized_pnl"] == pytest.approx(2 * 0.5 * 100)
    assert body["positions_value"] == pytest.approx(stock["market_value"] + 400.0)
    assert body["total_value"] == pytest.approx(body["cash"] + body["positions_value"])
    assert stock["multiplier"] == 1


def test_an_unquoted_option_is_unpriced_with_its_cost_at_the_multiplier(client, settings, seeded):
    _hold_an_option(settings)
    body = client.get("/api/portfolio", headers=AUTH).json()
    opt = next(p for p in body["positions"] if p["ticker"] == _OPTION)
    assert opt["price"] is None and opt["market_value"] is None
    assert opt["multiplier"] == 100
    assert opt["cost_basis"] == pytest.approx(2 * 1.5 * 100)
    assert opt["unrealized_pnl"] is None


def test_portfolio_currency_follows_the_instruments(client, settings):
    from stonks.store.lake import DuckDBLake

    lake = DuckDBLake(settings.lake.path)
    try:
        lake.sql("UPDATE instruments SET currency = 'EUR' WHERE id = 'UP.US'")
    finally:
        lake.close()
    body = client.get("/api/portfolio", headers=AUTH).json()
    assert body["currency"] == "EUR"
    assert body["positions"][0]["currency"] == "EUR"


def test_empty_book_defaults_to_usd(settings, fake_source):
    settings.api.allowed_hosts = ["testserver"]
    with TestClient(create_app(settings, source_factory=lambda: fake_source), client=LOOPBACK) as c:
        body = c.get("/api/portfolio", headers=AUTH).json()
    assert body["currency"] == "USD"
    assert body["positions"] == []
    assert body["unrealized_pnl"] == 0.0


# ---- ticks --------------------------------------------------------------------


def test_tick_runs_have_as_of_typed_status_and_iso_datetimes(client, seeded):
    [run] = client.get("/api/ticks", headers=AUTH).json()["items"]
    assert run["as_of"] == "2026-03-20"
    assert run["status"] == "ok"
    assert "T" in run["started_at"] and "T" in run["finished_at"]
    detail = client.get(f"/api/ticks/{seeded['tick_id']}", headers=AUTH).json()
    assert detail["as_of"] == "2026-03-20"
    assert client.get("/api/ticks", params={"status": "bogus"}, headers=AUTH).status_code == 422


def test_tick_schema_status_is_an_enum(client):
    schema = client.get("/openapi.json").json()["components"]["schemas"]["TickRun"]
    assert set(schema["properties"]["status"]["enum"]) == {"running", "ok", "partial", "error"}
    assert schema["properties"]["started_at"]["format"] == "date-time"


# ---- strategies summary -------------------------------------------------------


def test_strategy_summary_counts_by_status(client):
    resp = client.get("/api/strategies/summary")
    assert resp.status_code == 200
    assert resp.json() == {"active": 1, "shadow": 1, "retired": 0, "total": 2}


# ---- alerts -------------------------------------------------------------------


def _store_alerts(settings) -> None:
    notifier = StoreNotifier(settings.state.path, secrets=lambda: ["test-token-123"])
    for i, level in enumerate(["info", "warning", "error", "warning"]):
        notifier.notify(
            Notification(
                level=level,  # type: ignore[arg-type]
                title=f"alert {i}",
                message=f"message {i} test-token-123",
                fields={"i": i},
            )
        )


def test_alerts_are_listed_newest_first_and_paginated(client, settings):
    _store_alerts(settings)
    page = client.get("/api/alerts", headers=AUTH, params={"limit": 2}).json()
    assert page["total"] == 4
    assert [a["title"] for a in page["items"]] == ["alert 3", "alert 2"]
    first = page["items"][0]
    assert first["context"] == {"i": 3}
    assert "test-token-123" not in json.dumps(page)
    assert set(first) >= {"id", "level", "title", "message", "context", "created_at"}
    nxt = client.get("/api/alerts", headers=AUTH, params={"limit": 2, "offset": 2}).json()
    assert [a["title"] for a in nxt["items"]] == ["alert 1", "alert 0"]


def test_alerts_filter_by_level(client, settings):
    _store_alerts(settings)
    page = client.get("/api/alerts", headers=AUTH, params={"level": "warning"}).json()
    assert page["total"] == 2
    assert {a["level"] for a in page["items"]} == {"warning"}
    assert client.get("/api/alerts", headers=AUTH, params={"level": "bogus"}).status_code == 422


def test_alerts_need_token_remotely(remote):
    assert remote.get("/api/alerts").status_code == 401
    assert remote.get("/api/alerts", headers=AUTH).status_code == 200


def test_tick_alerts_land_in_the_store(client, settings):
    # The default backends include "store": a tick through the API persists
    # its notifications.
    with SqliteState(settings.state.path) as state:
        before = state.sql("SELECT COUNT(*) FROM alerts")[0][0]
    from stonks.production.settings_builder import build_tick_runtime

    runtime = build_tick_runtime(settings, ["UP.US"])
    runtime.notifier.notify(
        Notification(
            level="error",
            title="boom",
            message="auth failed with test-token-123",
            fields={"token": "test-token-123", "detail": "sent test-token-123"},
        )
    )
    with SqliteState(settings.state.path) as state:
        rows = state.sql("SELECT * FROM alerts ORDER BY id")
    assert len(rows) == before + 1
    # The configured API token is scrubbed before the row is written.
    assert "test-token-123" not in json.dumps([dict(r) for r in rows])

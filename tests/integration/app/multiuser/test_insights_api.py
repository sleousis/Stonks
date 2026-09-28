"""Portfolio insights over HTTP (roadmap 15.4): a simulated book and a synced
broker account, strategy agreement, and who may see what."""

from __future__ import annotations

import pytest

from stonks.accounts import PortfolioRepository, Role
from stonks.accounts.scope import Scope
from stonks.store.state import SqliteState
from tests.integration.app.test_api import AUTH


def _portfolio(path, user_id: str, name: str, kind: str = "simulated") -> str:
    with SqliteState(path) as state:
        scope = Scope(user_id=user_id, role=Role.TRADER)
        return PortfolioRepository(state).create(scope, name=name, kind=kind).id  # type: ignore[arg-type]


def _snapshot(path, portfolio_id: str, day: str, cash: float, positions: str, total: float,
              source: str = "tick") -> int:  # fmt: skip
    with SqliteState(path) as state:
        state.execute(
            "INSERT INTO portfolio_snapshots (taken_at, as_of, cash, positions_json, total_value,"
            " portfolio_id, source) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [f"{day}T21:00:00+00:00", day, cash, positions, total, portfolio_id, source],
        )
        return int(state.sql("SELECT MAX(id) FROM portfolio_snapshots")[0][0])


@pytest.fixture
def alice_book(settings, people) -> str:
    """Alice's simulated book: 10 UP.US and 100 FLAT.US plus cash, with two
    days of history."""
    pid = _portfolio(settings.state.path, people["alice"]["id"], "Alice main")
    _snapshot(settings.state.path, pid, "2026-03-30", 5_000.0, "{}", 5_000.0)
    _snapshot(
        settings.state.path, pid, "2026-03-31", 1_000.0, '{"UP.US": 10, "FLAT.US": 100}', 8_000.0
    )
    return pid


def test_insights_of_a_simulated_book(client, people, alice_book):
    alice = people["alice"]["headers"]
    body = client.get(
        "/api/insights", params={"portfolio_id": alice_book, "benchmark": "UP.US"}, headers=alice
    ).json()
    assert body["portfolio_id"] == alice_book and body["source"] == "tick"
    tickers = {s["key"]: s for s in body["allocation"]["ticker"]}
    assert set(tickers) == {"UP.US", "FLAT.US", "cash"}
    assert tickers["FLAT.US"]["value"] == pytest.approx(5_000.0)
    classes = {s["key"] for s in body["allocation"]["asset_class"]}
    assert classes == {"equity", "cash"}
    assert body["exposure"]["benchmark"] == "UP.US"
    assert body["exposure"]["gross"] == pytest.approx(body["exposure"]["net"])
    # UP.US has beta 1 to itself; FLAT.US never moves, so beta 0.
    up_weight = tickers["UP.US"]["weight"]
    assert body["exposure"]["beta"] == pytest.approx(up_weight)
    periods = {p["period"]: p for p in body["pnl"]}
    assert periods["1d"]["change"] == pytest.approx(3_000.0)
    assert periods["1m"]["change"] is None
    assert body["risk"]["concentration"]["holdings"] == 2
    assert body["risk"]["holdings"]["observations"] > 20
    assert body["risk"]["history"] is None  # two days are not enough


def test_missing_benchmark_is_a_note_not_an_error(client, people, alice_book):
    body = client.get(
        "/api/insights", params={"portfolio_id": alice_book}, headers=people["alice"]["headers"]
    ).json()
    assert body["exposure"]["beta"] is None
    assert any("SPY.US" in n for n in body["notes"])


def test_insights_of_a_synced_broker_account(client, settings, people):
    path = settings.state.path
    pid = _portfolio(path, people["alice"]["id"], "Broker", kind="broker")
    snap = _snapshot(path, pid, "2026-03-31", 500.0, '{"UP.US": 2}', 1_100.0, source="sync")
    with SqliteState(path) as state:
        for raw, ticker, qty, price, value in [
            ("UP", "UP.US", 2, 150.0, 300.0),
            ("XYZ123", None, 3, 100.0, 300.0),
        ]:
            state.execute(
                "INSERT INTO broker_positions (snapshot_id, portfolio_id, raw_symbol, ticker,"
                " quantity, price, market_value, currency) VALUES (?, ?, ?, ?, ?, ?, ?, 'usd')",
                [snap, pid, raw, ticker, qty, price, value],
            )
    body = client.get(
        "/api/insights", params={"portfolio_id": pid}, headers=people["alice"]["headers"]
    ).json()
    assert body["source"] == "sync"
    assert body["uncovered"] == ["XYZ123"]
    assert body["total_value"] == pytest.approx(1_100.0)
    tickers = {s["key"]: s["value"] for s in body["allocation"]["ticker"]}
    assert tickers == {"UP.US": 300.0, "XYZ123": 300.0, "cash": 500.0}
    currencies = {s["key"] for s in body["allocation"]["currency"]}
    assert currencies == {"USD"}
    assert any("no ticker" in n for n in body["notes"])

    agreement = client.get(
        "/api/insights/agreement", params={"portfolio_id": pid}, headers=people["alice"]["headers"]
    ).json()
    rows = {r["symbol"]: r for r in agreement["holdings"]}
    assert rows["XYZ123"]["opinions"][0]["stance"] == "not_applicable"


def test_strategy_agreement_of_the_default_book(client):
    body = client.get("/api/insights/agreement", headers=AUTH).json()
    assert body["portfolio_id"] == "pf_default"
    assert body["strategies"] == ["bah_active"]
    up = next(r for r in body["holdings"] if r["symbol"] == "UP.US")
    opinion = up["opinions"][0]
    assert opinion["strategy_id"] == "bah_active" and opinion["stance"] == "agree"
    assert "UP.US" in opinion["reason"] and up["agree"] == 1
    assert body["as_of"] is not None


def test_empty_portfolio(client, settings, people):
    pid = _portfolio(settings.state.path, people["bob"]["id"], "Empty")
    body = client.get(
        "/api/insights", params={"portfolio_id": pid}, headers=people["bob"]["headers"]
    ).json()
    assert body["source"] is None and body["pnl"] == []
    assert body["risk"]["concentration"]["holdings"] == 0
    agreement = client.get(
        "/api/insights/agreement", params={"portfolio_id": pid}, headers=people["bob"]["headers"]
    ).json()
    assert agreement["holdings"] == [] and agreement["as_of"] is None


@pytest.mark.parametrize("route", ["/api/insights", "/api/insights/agreement"])
@pytest.mark.parametrize("who", ["bob", "vic", "ada"])
def test_other_peoples_insights_are_not_found(client, people, alice_book, route, who):
    resp = client.get(route, params={"portfolio_id": alice_book}, headers=people[who]["headers"])
    assert resp.status_code == 404
    assert "UP.US" not in resp.text


def test_admins_get_totals_without_holdings(client, people, alice_book, settings):
    # three traders besides the admin, so the sums reveal nobody (BE-43)
    for who in ("bob", "vic"):
        pid = _portfolio(settings.state.path, people[who]["id"], f"{who} main")
        _snapshot(settings.state.path, pid, "2026-03-31", 100.0, "{}", 100.0)
    resp = client.get("/api/insights/totals", headers=people["ada"]["headers"])
    assert resp.status_code == 200
    body = resp.json()
    assert body["portfolios"] >= 2 and body["owners"] >= 2
    assert "UP.US" not in resp.text and "FLAT.US" not in resp.text
    assert {s["key"] for s in body["asset_class"]} <= {"equity", "cash", "unknown"}
    for who in ("alice", "vic"):
        denied = client.get("/api/insights/totals", headers=people[who]["headers"])
        assert denied.status_code == 403


def test_insights_need_a_credential(client, alice_book):
    assert client.get("/api/insights").status_code == 401


def test_look_through_splits_a_held_fund(client, settings, people):
    """Alice holds UP.US and FLAT.US, and FLAT.US is a fund that owns half
    UP.US (roadmap 23.14)."""
    from datetime import date, datetime

    import pandas as pd

    from stonks.store.lake import DuckDBLake

    pid = _portfolio(settings.state.path, people["alice"]["id"], "Alice funds")
    _snapshot(settings.state.path, pid, "2026-03-31", 0.0, '{"UP.US": 10, "FLAT.US": 100}', 1.0)
    lake = DuckDBLake(settings.lake.path)
    lake.upsert_fund_holdings(
        pd.DataFrame(
            [
                {
                    "fund": "FLAT.US",
                    "holding": "UP.US",
                    "as_of": date(2026, 3, 1),
                    "source": "fake",
                    "weight": 0.5,
                    "sector": "Technology",
                    "country": "US",
                },
            ]
        ),
        known_at=datetime(2026, 3, 2),
    )
    lake.close()
    alice = people["alice"]["headers"]
    res = client.get("/api/insights/look-through", params={"portfolio_id": pid}, headers=alice)
    assert res.status_code == 200, res.text
    body = res.json()
    lt = body["look_through"]
    up = next(n for n in lt["names"] if n["key"] == "UP.US")
    flat_value = lt["funds"][0]["value"]
    assert lt["funds"][0]["fund"] == "FLAT.US"
    assert up["fund_value"] == pytest.approx(flat_value * 0.5)
    assert up["funds"] == ["FLAT.US"]
    assert any(s["key"] == "not listed" for s in lt["sector"])
    assert any("not listed" in n for n in body["notes"])
    # Bob may not read Alice's book.
    bob = people["bob"]["headers"]
    other = client.get("/api/insights/look-through", params={"portfolio_id": pid}, headers=bob)
    assert other.status_code == 404


def _manual_fill(path, pid: str, cid: str, side: str, qty: float, price: float, at: str) -> None:
    with SqliteState(path) as state:
        state.execute(
            "INSERT INTO orders (client_id, ticker, side, quantity, order_type, status,"
            " created_at, updated_at, portfolio_id, origin) VALUES (?, 'UP.US', ?, ?, 'market',"
            " 'filled', ?, ?, ?, 'manual')",
            [cid, side, qty, at, at, pid],
        )
        state.execute(
            "INSERT INTO fills (order_client_id, ticker, quantity, price, fee, filled_at,"
            " portfolio_id) VALUES (?, 'UP.US', ?, ?, 0, ?, ?)",
            [cid, qty, price, at, pid],
        )


def test_behaviour_report_of_manual_trades(client, people, settings, alice_book):
    path = settings.state.path
    _manual_fill(path, alice_book, "manual:a:1", "buy", 10, 100.0, "2026-03-02T15:00:00+00:00")
    _manual_fill(path, alice_book, "manual:a:2", "sell", 10, 90.0, "2026-03-04T15:00:00+00:00")
    _manual_fill(path, alice_book, "manual:a:3", "buy", 5, 90.0, "2026-03-04T16:00:00+00:00")
    _manual_fill(path, alice_book, "manual:a:4", "sell", 5, 95.0, "2026-03-05T15:00:00+00:00")
    alice = people["alice"]["headers"]
    r = client.get("/api/insights/behaviour", params={"portfolio_id": alice_book}, headers=alice)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["trades"] == 2 and body["win_rate"] == 0.5
    assert body["total_pnl"] == -100.0 + 25.0
    assert body["revenge"]["trades"] == 1
    assert body["sources"] == {"manual": 4}
    assert {b["label"] for b in body["versus_strategies"]} == {"with", "against", "no_view"}
    later = client.get(
        "/api/insights/behaviour",
        params={"portfolio_id": alice_book, "since": "2026-03-05"},
        headers=alice,
    ).json()
    assert later["trades"] == 0
    bob = client.get(
        "/api/insights/behaviour",
        params={"portfolio_id": alice_book},
        headers=people["bob"]["headers"],
    )
    assert bob.status_code == 404


def test_behaviour_since_pairs_exits_with_entries_made_before_it(
    client, people, settings, alice_book
):
    """``since`` picks the trades entered on or after it. A sell of shares
    bought before it closes that older lot: it opens no short, and the
    buy back after it is a new long, not the cover of a phantom short."""
    path = settings.state.path
    _manual_fill(path, alice_book, "manual:s:1", "buy", 10, 100.0, "2026-01-05T15:00:00+00:00")
    _manual_fill(path, alice_book, "manual:s:2", "sell", 10, 110.0, "2026-02-02T15:00:00+00:00")
    _manual_fill(path, alice_book, "manual:s:3", "buy", 10, 105.0, "2026-02-10T15:00:00+00:00")
    _manual_fill(path, alice_book, "manual:s:4", "sell", 10, 120.0, "2026-02-20T15:00:00+00:00")
    body = client.get(
        "/api/insights/behaviour",
        params={"portfolio_id": alice_book, "since": "2026-02-01"},
        headers=people["alice"]["headers"],
    ).json()
    assert body["trades"] == 1
    assert body["total_pnl"] == pytest.approx(150.0)
    assert body["open_positions"] == 0


def test_dashboard_and_insights_share_one_day_change(client, people, alice_book):
    """Visual audit M2: the P&L series (Dashboard, Today) and Insights read
    the headline value and day change from one service, so they agree."""
    alice = people["alice"]["headers"]
    pnl = client.get("/api/pnl", params={"portfolio_id": alice_book}, headers=alice).json()
    insights = client.get(
        "/api/insights", params={"portfolio_id": alice_book}, headers=alice
    ).json()
    assert pnl["day_change"] == insights["day_change"]
    assert insights["day_change"] == {
        "day": "2026-03-31",
        "previous_day": "2026-03-30",
        "value": 8_000.0,
        "change": 3_000.0,
        "change_pct": pytest.approx(0.6),
    }
    one_d = {p["period"]: p for p in insights["pnl"]}["1d"]
    assert one_d["change"] == insights["day_change"]["change"]
    assert one_d["change_pct"] == insights["day_change"]["change_pct"]


def test_the_day_change_is_empty_on_both_across_a_gap(client, settings, people):
    pid = _portfolio(settings.state.path, people["alice"]["id"], "Gappy")
    _snapshot(settings.state.path, pid, "2026-03-01", 100.0, "{}", 100.0)
    _snapshot(settings.state.path, pid, "2026-03-31", 90.0, "{}", 90.0)
    alice = people["alice"]["headers"]
    pnl = client.get("/api/pnl", params={"portfolio_id": pid}, headers=alice).json()
    insights = client.get("/api/insights", params={"portfolio_id": pid}, headers=alice).json()
    assert pnl["day_change"] == insights["day_change"]
    assert insights["day_change"]["change"] is None
    assert {p["period"]: p for p in insights["pnl"]}["1d"]["change"] is None

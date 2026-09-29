"""Trader-ready UX routes (roadmap 13.2, 13.4, 13.5, 13.6, 13.8, 13.12):
the first-run guide, watchlists, charts, the leaderboard and tear sheets,
your own risk limits and CSV exports. Every route is scoped to the caller."""

from __future__ import annotations

import csv
import io
import json
from datetime import UTC, datetime

import pytest

from stonks.store.state import SqliteState

LEGACY = {"Authorization": "Bearer test-token-123"}  # the bootstrap admin, owns pf_default


def _rows(text: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(text)))


# ---- watchlists (13.4) -----------------------------------------------------------


def test_watchlists_are_yours_alone(client, people):
    alice, bob = people["alice"]["headers"], people["bob"]["headers"]
    made = client.post(
        "/api/watchlists",
        json={"name": " Tech ", "tickers": ["aapl.us", " MSFT.US", "AAPL.US", ""]},
        headers=alice,
    )
    assert made.status_code == 201, made.text
    wl = made.json()
    assert wl["name"] == "Tech" and wl["tickers"] == ["AAPL.US", "MSFT.US"]
    wid = wl["id"]

    assert client.get("/api/watchlists", headers=alice).json()["total"] == 1
    assert client.get("/api/watchlists", headers=bob).json()["total"] == 0
    assert client.get(f"/api/watchlists/{wid}", headers=bob).status_code == 404
    assert (
        client.patch(f"/api/watchlists/{wid}", json={"name": "x"}, headers=bob).status_code == 404
    )
    assert client.delete(f"/api/watchlists/{wid}", headers=bob).status_code == 404
    assert client.get(f"/api/watchlists/{wid}", headers=people["ada"]["headers"]).status_code == 404

    edited = client.patch(
        f"/api/watchlists/{wid}", json={"tickers": ["UP.US", "DOWN.US"]}, headers=alice
    )
    assert edited.status_code == 200 and edited.json()["tickers"] == ["UP.US", "DOWN.US"]
    assert edited.json()["name"] == "Tech"

    clash = client.post("/api/watchlists", json={"name": "Tech"}, headers=alice)
    assert clash.status_code == 409
    other = client.post("/api/watchlists", json={"name": "Tech"}, headers=bob)
    assert other.status_code == 201  # names are unique per person only

    assert client.delete(f"/api/watchlists/{wid}", headers=alice).status_code == 204
    assert client.get(f"/api/watchlists/{wid}", headers=alice).status_code == 404


def test_watchlist_validation_and_viewers(client, people):
    alice = people["alice"]["headers"]
    bad = client.post("/api/watchlists", json={"name": "x", "tickers": ["=cmd()"]}, headers=alice)
    assert bad.status_code == 422
    blank = client.post("/api/watchlists", json={"name": "   "}, headers=alice)
    assert blank.status_code == 422
    vic = people["vic"]["headers"]
    assert client.post("/api/watchlists", json={"name": "v"}, headers=vic).status_code == 403
    assert client.get("/api/watchlists", headers=vic).status_code == 200


# ---- onboarding (13.2) -----------------------------------------------------------


def test_onboarding_steps_are_stored_per_person(client, people):
    alice, bob = people["alice"]["headers"], people["bob"]["headers"]
    first = client.get("/api/onboarding", headers=alice).json()
    assert [s["id"] for s in first["steps"]] == ["account", "portfolio", "data", "follow", "alerts"]
    assert first["show"] is True and first["complete"] is False
    assert all(s["state"] == "todo" for s in first["steps"])

    skipped = client.put("/api/onboarding/steps/alerts", json={"state": "skipped"}, headers=alice)
    assert skipped.status_code == 200, skipped.text
    assert {s["id"]: s["state"] for s in skipped.json()["steps"]}["alerts"] == "skipped"
    # Bob's guide is his own.
    bob_view = client.get("/api/onboarding", headers=bob).json()
    assert {s["id"]: s["state"] for s in bob_view["steps"]}["alerts"] == "todo"

    # A watchlist shows the data step done, whatever was stored.
    client.post("/api/watchlists", json={"name": "Mine", "tickers": ["UP.US"]}, headers=alice)
    view = client.put("/api/onboarding/steps/data", json={"state": "todo"}, headers=alice).json()
    data = next(s for s in view["steps"] if s["id"] == "data")
    assert data == {"id": "data", "state": "done", "derived": True}

    back = client.put("/api/onboarding/steps/alerts", json={"state": "todo"}, headers=alice)
    assert {s["id"]: s["state"] for s in back.json()["steps"]}["alerts"] == "todo"

    closed = client.put("/api/onboarding", json={"dismissed": True}, headers=alice).json()
    assert closed["dismissed"] is True and closed["show"] is False
    reopened = client.put("/api/onboarding", json={"dismissed": False}, headers=alice).json()
    assert reopened["show"] is True

    unknown = client.put("/api/onboarding/steps/nope", json={"state": "done"}, headers=alice)
    assert unknown.status_code == 422


def test_onboarding_counts_a_linked_telegram_chat_as_alerts_on(client, people, settings):
    """Alerts can reach you on Telegram alone, with no push device
    (docs/without-a-broker.md)."""
    alice = people["alice"]
    with SqliteState(settings.state.path) as state:
        state.execute(
            "INSERT INTO telegram_links (chat_id, user_id, username, linked_at)"
            " VALUES ('4242', ?, 'alice_tg', ?)",
            [alice["id"], datetime.now(UTC).isoformat()],
        )
    view = client.get("/api/onboarding", headers=alice["headers"]).json()
    alerts = next(s for s in view["steps"] if s["id"] == "alerts")
    assert alerts == {"id": "alerts", "state": "done", "derived": True}


def test_onboarding_completes_when_every_step_is_done_or_skipped(client, people):
    vic = people["vic"]["headers"]  # viewers can use the guide too
    for step in ("account", "portfolio", "data", "follow"):
        assert (
            client.put(f"/api/onboarding/steps/{step}", json={"state": "done"}, headers=vic)
        ).status_code == 200
    last = client.put("/api/onboarding/steps/alerts", json={"state": "skipped"}, headers=vic)
    assert last.json()["complete"] is True and last.json()["show"] is False


def test_the_system_checklist_is_for_admins(client, people):
    assert (
        client.get("/api/onboarding/system", headers=people["alice"]["headers"]).status_code == 403
    )
    got = client.get("/api/onboarding/system", headers=people["ada"]["headers"])
    assert got.status_code == 200, got.text
    checks = {c["id"]: c for c in got.json()["checks"]}
    assert set(checks) == {"data_source", "first_ingest", "strategies", "backup", "scheduler"}
    assert checks["strategies"]["done"] is True  # the seeded strategies
    assert checks["backup"]["done"] is False
    assert checks["scheduler"]["done"] is False and checks["scheduler"]["detail"]
    assert got.json()["complete"] is False


def test_the_data_source_check_passes_on_yahoo_with_no_key(client, people, monkeypatch):
    """No EODHD key: the daily price update reads Yahoo, which needs none,
    so the install has a working data source (docs/without-a-broker.md)."""
    monkeypatch.delenv("EODHD_API_KEY", raising=False)
    got = client.get("/api/onboarding/system", headers=people["ada"]["headers"]).json()
    check = next(c for c in got["checks"] if c["id"] == "data_source")
    assert check["done"] is True, check
    assert "yahoo" in check["detail"]


# ---- charts (13.5) ---------------------------------------------------------------


def _signal(settings, ticker: str, kind: str = "entry") -> None:
    with SqliteState(settings.state.path) as state:
        state.execute(
            "INSERT OR REPLACE INTO signal_events (as_of, strategy_id, ticker, kind, tick_id, strength,"
            " reason_json, created_at) VALUES ('2026-03-20', 'bah_active', ?, ?, 't', 0.5, ?, ?)",
            [ticker, kind, json.dumps({"text": "trend is up"}), datetime.now(UTC).isoformat()],
        )


def test_a_chart_has_bars_your_fills_and_signals(client, settings, people):
    _signal(settings, "UP.US")
    own = client.get("/api/charts/UP.US", headers=LEGACY)
    assert own.status_code == 200, own.text
    body = own.json()
    assert body["ticker"] == "UP.US" and body["interval"] == "1d" and len(body["bars"]) > 100
    assert body["portfolio_id"] == "pf_default"
    assert body["fills"] and body["fills"][0]["side"] == "buy"
    assert body["signals"] == [
        {
            "as_of": "2026-03-20",
            "strategy_id": "bah_active",
            "kind": "entry",
            "strength": 0.5,
            "reason": "trend is up",
        }
    ]
    limited = client.get("/api/charts/UP.US", params={"limit": 10}, headers=LEGACY).json()
    assert len(limited["bars"]) == 10 and limited["truncated"] is True

    # Alice has no portfolio: bars and signals, no fills. Another book is a 404.
    alice = people["alice"]["headers"]
    hers = client.get("/api/charts/UP.US", headers=alice).json()
    assert hers["fills"] == [] and hers["portfolio_id"] is None and hers["signals"]
    spy = client.get("/api/charts/UP.US", params={"portfolio_id": "pf_default"}, headers=alice)
    assert spy.status_code == 404

    other = client.get(
        "/api/charts/UP.US", params={"strategy_id": "bah_shadow"}, headers=LEGACY
    ).json()
    assert other["signals"] == []
    empty = client.get("/api/charts/NOPE.US", headers=LEGACY).json()
    assert empty["bars"] == [] and empty["fills"] == [] and empty["signals"] == []


def test_compare_rebases_tickers_with_drawdown_and_rolling_sharpe(client, people):
    alice = people["alice"]["headers"]
    resp = client.get(
        "/api/charts/compare",
        params={"tickers": "up.us, DOWN.US,NOPE.US,UP.US", "window": 20},
        headers=alice,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["window"] == 20 and body["missing"] == ["NOPE.US"]
    up, down = body["series"]
    assert (up["ticker"], down["ticker"]) == ("UP.US", "DOWN.US")
    assert up["points"][0]["time"] == down["points"][0]["time"] == body["start"]
    assert up["points"][0]["value"] == down["points"][0]["value"] == 100.0
    assert up["total_return"] > 0 > down["total_return"]
    assert up["max_drawdown"] == 0.0 and down["max_drawdown"] < 0
    assert down["drawdown"][-1]["value"] == pytest.approx(down["total_return"])
    assert up["rolling_sharpe"] and up["sharpe"] > 0 and down["sharpe"] < 0
    assert up["periods_per_year"] == 252
    # every bar is shown: the rolling Sharpe waits for a full window
    assert len(up["rolling_sharpe"]) == len(up["points"]) - 20
    # a shorter range reads the window's bars before it, so it starts on day one
    short = client.get(
        "/api/charts/compare", params={"tickers": "UP.US", "limit": 30}, headers=alice
    ).json()
    series = short["series"][0]
    assert len(series["points"]) == 30
    assert series["rolling_sharpe"][0]["time"] == short["start"]
    assert len(series["rolling_sharpe"]) == 30

    too_many = client.get("/api/charts/compare", params={"tickers": "A,B,C,D,E,F,G"}, headers=alice)
    assert too_many.status_code == 422
    blank = client.get("/api/charts/compare", params={"tickers": " , "}, headers=alice)
    assert blank.status_code == 422
    none = client.get("/api/charts/compare", params={"tickers": "NOPE.US"}, headers=alice).json()
    assert none["series"] == [] and none["start"] is None and none["missing"] == ["NOPE.US"]


# ---- leaderboard and tear sheets (13.6) ------------------------------------------


def _shadow_book(settings, strategy_id: str, values: list[float]) -> None:
    with SqliteState(settings.state.path) as state:
        tick = state.sql("SELECT id FROM tick_runs LIMIT 1")[0]["id"]
        state.execute("DELETE FROM shadow_portfolio_snapshots WHERE strategy_id = ?", [strategy_id])
        for i, v in enumerate(values):
            day = f"2026-02-{i + 2:02d}"
            state.execute(
                "INSERT INTO shadow_portfolio_snapshots (tick_id, strategy_id, as_of, taken_at,"
                " cash, positions_json, total_value) VALUES (?, ?, ?, ?, 0, '{}', ?)",
                [tick, strategy_id, day, f"{day}T21:00:00+00:00", v],
            )
        state.execute(
            "INSERT INTO shadow_decisions (tick_id, strategy_id, as_of, ticker, side, quantity,"
            " price, status, created_at) VALUES (?, ?, '2026-02-02', 'DOWN.US', 'buy', 10, 90,"
            " 'filled', '2026-02-02T21:00:00+00:00')",
            [tick, strategy_id],
        )


def test_the_leaderboard_ranks_by_risk_adjusted_paper_result(client, settings, people):
    _shadow_book(settings, "bah_shadow", [10_000, 10_100, 10_050, 10_200, 10_300])
    _shadow_book(settings, "bah_active", [10_000, 9_900, 9_950, 9_800, 9_700])
    got = client.get("/api/strategies/leaderboard", headers=people["vic"]["headers"])
    assert got.status_code == 200, got.text
    rows = got.json()["rows"]
    assert [r["strategy_id"] for r in rows] == ["bah_shadow", "bah_active"]
    assert [r["rank"] for r in rows] == [1, 2]
    top = rows[0]
    assert top["paper"]["days"] == 5 and top["paper"]["trades"] == 1
    assert top["paper"]["total_return"] == pytest.approx(0.03)
    assert top["paper"]["sharpe"] > 0 and top["paper"]["max_drawdown"] < 0
    assert top["golive_passed"] in (True, False)
    active = rows[1]
    assert active["status"] == "active" and active["book_trades"] >= 1
    assert active["live_since"] is not None
    assert (active["survival_passed"], active["survival_total"]) == (1, 1)

    by_dd = client.get(
        "/api/strategies/leaderboard", params={"sort": "drawdown"}, headers=LEGACY
    ).json()
    assert by_dd["sort"] == "drawdown" and by_dd["rows"][0]["strategy_id"] == "bah_shadow"


def test_a_tear_sheet_gathers_one_strategy(client, settings, people):
    _shadow_book(settings, "bah_shadow", [10_000, 10_100, 10_050, 10_200])
    got = client.get("/api/strategies/bah_shadow/tearsheet", headers=people["alice"]["headers"])
    assert got.status_code == 200, got.text
    sheet = got.json()
    assert sheet["strategy"]["id"] == "bah_shadow"
    assert len(sheet["curve"]) == 4 and sheet["paper"]["days"] == 4
    [month] = sheet["monthly_returns"]
    assert month["month"] == "2026-02" and month["value"] == pytest.approx(0.02)
    assert sheet["recent_trades"][0]["ticker"] == "DOWN.US"
    assert sheet["golive"]["strategy_id"] == "bah_shadow"
    assert client.get("/api/strategies/nope/tearsheet", headers=LEGACY).status_code == 404


# ---- risk limits (13.8) ----------------------------------------------------------


def test_your_risk_limits_tighten_the_system_policy(client, settings, people):
    alice = people["alice"]["headers"]
    first = client.get("/api/risk/limits", headers=alice).json()
    assert first["mine"] == {} and first["ignored"] == []
    assert first["effective"] == first["system"]

    saved = client.put(
        "/api/risk/limits",
        json={"limits": {"max_weight_per_ticker": 0.2, "max_open_positions": 8}},
        headers=alice,
    )
    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert body["mine"] == {"max_open_positions": 8, "max_weight_per_ticker": 0.2}
    assert body["effective"]["max_weight_per_ticker"] == 0.2
    assert body["effective"]["max_open_positions"] == 8
    # Bob's limits are untouched.
    assert client.get("/api/risk/limits", headers=people["bob"]["headers"]).json()["mine"] == {}
    with SqliteState(settings.state.path) as state:
        audit = state.sql("SELECT action FROM audit_log WHERE action = 'user.risk_policy'")
    assert len(audit) == 1

    bad = client.put(
        "/api/risk/limits", json={"limits": {"max_weight_per_ticker": 2}}, headers=alice
    )
    assert bad.status_code == 422
    unknown = client.put("/api/risk/limits", json={"limits": {"nope": 1}}, headers=alice)
    assert unknown.status_code == 422
    vic = people["vic"]["headers"]
    assert client.put("/api/risk/limits", json={"limits": {}}, headers=vic).status_code == 403
    cleared = client.put("/api/risk/limits", json={"limits": {}}, headers=alice).json()
    assert cleared["mine"] == {}


def test_a_looser_limit_is_listed_as_ignored(client, settings, people):
    settings.production.risk.max_weight_per_ticker = 0.3
    alice = people["alice"]["headers"]
    body = client.put(
        "/api/risk/limits", json={"limits": {"max_weight_per_ticker": 0.5}}, headers=alice
    ).json()
    assert body["ignored"] == ["max_weight_per_ticker"]
    assert body["effective"]["max_weight_per_ticker"] == 0.3


# ---- exports (13.12) -------------------------------------------------------------


def test_portfolio_exports_are_csv_of_your_book(client, people):
    for kind in ("orders", "fills", "journal", "snapshots", "pnl"):
        got = client.get(f"/api/exports/{kind}", headers=LEGACY)
        assert got.status_code == 200, (kind, got.text)
        assert got.headers["content-type"].startswith("text/csv")
        disposition = got.headers["content-disposition"]
        assert disposition.startswith("attachment;") and f"stonks-{kind}-pf_default-" in disposition
        rows = _rows(got.text)
        assert rows, kind
    orders = _rows(client.get("/api/exports/orders", headers=LEGACY).text)
    assert orders[0]["ticker"] == "UP.US" and orders[0]["side"] == "buy"
    fills = _rows(client.get("/api/exports/fills", headers=LEGACY).text)
    assert float(fills[0]["price"]) > 0

    alice = people["alice"]["headers"]
    spy = client.get("/api/exports/orders", params={"portfolio_id": "pf_default"}, headers=alice)
    assert spy.status_code == 404
    none = client.get("/api/exports/orders", headers=alice)
    assert none.status_code == 404  # no portfolio yet


def test_lab_trials_export_needs_the_lab(client, settings, people):
    from stonks.lab.trials import LabRunSpec, TrialLedger, TrialRecord

    with SqliteState(settings.state.path) as state:
        run_id = TrialLedger(state, settings.registry.artifacts_dir).record_run(
            LabRunSpec(
                strategy_class="stonks.strategies.examples.momentum:Momentum",
                hypothesis="=HYPERLINK()",
                premortem="x",
                tuner="grid",
                objective="sharpe",
                budget=2,
                seed=0,
                dataset={"universe": ["UP.US"]},
            ),
            [
                TrialRecord(trial_index=0, params={"lookback_days": 5}, score=0.4),
                TrialRecord(trial_index=1, params={"lookback_days": 9}, score=-0.1),
            ],
            verdict="fail",
        )
    alice = people["alice"]["headers"]
    got = client.get("/api/exports/lab-trials", params={"run_id": run_id}, headers=alice)
    assert got.status_code == 200, got.text
    rows = _rows(got.text)
    assert [r["trial_index"] for r in rows] == ["0", "1"]
    assert json.loads(rows[0]["params"]) == {"lookback_days": 5}
    assert rows[1]["score"] == "-0.1"  # numbers are never escaped
    everything = _rows(client.get("/api/exports/lab-trials", headers=alice).text)
    assert len(everything) == 2
    assert (
        client.get("/api/exports/lab-trials", headers=people["vic"]["headers"]).status_code == 403
    )


def test_csv_cells_that_look_like_formulas_are_escaped():
    from stonks.app.exports import filename, to_csv

    text = to_csv(["a", "b", "c"], [["=1+1", -2.5, {"k": [1]}], [None, True, "@x"]])
    rows = list(csv.reader(io.StringIO(text)))
    assert rows[1] == ["'=1+1", "-2.5", '{"k": [1]}']
    assert rows[2] == ["", "true", "'@x"]
    assert filename("orders", 'pf "x"/y').startswith("stonks-orders-pf--x--y-")

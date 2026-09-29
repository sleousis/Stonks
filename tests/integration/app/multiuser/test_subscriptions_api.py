"""S7: your portfolios, whether each trades paper or live, and your
subscriptions (list, subscribe, enable, disable, change mode with the auto
gate)."""

from __future__ import annotations

import pytest

from stonks.accounts import DEFAULT_PORTFOLIO_ID, PortfolioRepository, Scope
from stonks.store.state import SqliteState
from tests.fixtures.paper import link_connection, seed_paper_days
from tests.integration.app.stepup import allow_step_up
from tests.integration.app.test_api import AUTH


def _portfolio(settings, person: dict, name: str, kind: str = "simulated") -> str:
    with SqliteState(settings.state.path) as state:
        scope = Scope(user_id=person["id"], role=person["role"])
        return PortfolioRepository(state).create(scope, name=name, kind=kind).id


# ---- portfolios ------------------------------------------------------------------


def test_portfolios_lists_only_your_own(client, settings, people):
    alice_pf = _portfolio(settings, people["alice"], "Alice book")
    _portfolio(settings, people["bob"], "Bob book")
    alice = client.get("/api/portfolios", headers=people["alice"]["headers"])
    assert alice.status_code == 200
    assert [p["id"] for p in alice.json()["items"]] == [alice_pf]
    assert alice.json()["items"][0]["trading"] == "paper"
    assert "Bob book" not in alice.text
    # Admins see their own books too, never other people's.
    ada = client.get("/api/portfolios", headers=people["ada"]["headers"]).json()
    assert ada["items"] == [] and ada["total"] == 0
    owner = client.get("/api/portfolios", headers=AUTH).json()["items"]
    assert [p["id"] for p in owner] == [DEFAULT_PORTFOLIO_ID]


def test_portfolios_mark_the_one_reads_use_by_default(client, settings, people):
    _portfolio(settings, people["alice"], "First")
    _portfolio(settings, people["alice"], "Second")
    listed = client.get("/api/portfolios", headers=people["alice"]["headers"]).json()["items"]
    assert sum(p["is_default"] for p in listed) == 1
    owner = client.get("/api/portfolios", headers=AUTH).json()["items"]
    assert owner[0]["is_default"] is True


def test_trading_modes_say_paper_or_live_per_portfolio(client, settings, people):
    sim = _portfolio(settings, people["alice"], "Sim")
    mirror = _portfolio(settings, people["alice"], "Mirror", kind="broker")
    modes = client.get("/api/portfolios/trading-modes", headers=people["alice"]["headers"])
    assert modes.status_code == 200, modes.text
    by_id = {m["portfolio_id"]: m for m in modes.json()["items"]}
    assert set(by_id) == {sim, mirror}
    assert (by_id[sim]["trading"], by_id[sim]["broker"]) == ("paper", "simulated")
    # Real money follows the portfolio stage only: a broker portfolio at
    # Simulated or Broker paper is paper (docs/design/vocabulary.md).
    assert (by_id[mirror]["trading"], by_id[mirror]["broker"]) == ("paper", "connection")
    assert by_id[mirror]["live_stage"] == "sim_paper"
    default = client.get("/api/portfolios/trading-modes", headers=AUTH).json()["items"]
    assert [(m["portfolio_id"], m["trading"], m["broker"]) for m in default] == [
        (DEFAULT_PORTFOLIO_ID, "paper", "simulated")
    ]


@pytest.mark.parametrize(
    ("stages", "trading"),
    [
        (("broker_paper",), "paper"),
        (("broker_paper", "live_small"), "live"),
        (("broker_paper", "live_small", "live_scale"), "live"),
    ],
)
def test_a_broker_portfolio_is_live_only_at_a_real_money_stage(
    client, settings, people, stages, trading
):
    from stonks.production.live.stages import change_stage

    mirror = _portfolio(settings, people["alice"], "Mirror", kind="broker")
    with SqliteState(settings.state.path) as state:
        for stage in stages:
            change_stage(
                state, mirror, stage, actor="t", reason="setup",
                gate_report={"target": stage, "passed": True},
            )  # fmt: skip
    headers = people["alice"]["headers"]
    [mode] = client.get("/api/portfolios/trading-modes", headers=headers).json()["items"]
    assert (mode["trading"], mode["live_stage"]) == (trading, stages[-1])
    [book] = client.get("/api/portfolios", headers=headers).json()["items"]
    assert (book["trading"], book["live_stage"]) == (trading, stages[-1])


@pytest.mark.parametrize(
    ("paper", "allow_live", "trading"), [(True, False, "paper"), (False, True, "live")]
)
def test_the_default_book_follows_the_alpaca_endpoint(client, settings, paper, allow_live, trading):
    settings.brokers.kind = "alpaca"
    settings.brokers.alpaca.paper = paper
    settings.brokers.alpaca.allow_live = allow_live
    [mode] = client.get("/api/portfolios/trading-modes", headers=AUTH).json()["items"]
    assert (mode["trading"], mode["broker"]) == (trading, "alpaca")


@pytest.mark.parametrize(
    ("gateway", "allow_live", "trading"),
    [("live", True, "live"), ("paper", True, "paper"), ("live", False, "paper")],
)
def test_the_default_book_follows_its_ib_gateway(client, settings, gateway, allow_live, trading):
    from stonks.execution.brokers.ibkr.settings import IbkrGatewayConfig

    settings.brokers.kind = "ibkr"
    settings.brokers.ibkr.allow_live = allow_live
    settings.brokers.ibkr.gateways = {
        "main": IbkrGatewayConfig(
            host="127.0.0.1", port=4001, mode=gateway, portfolios=[DEFAULT_PORTFOLIO_ID]
        )
    }
    [mode] = client.get("/api/portfolios/trading-modes", headers=AUTH).json()["items"]
    assert (mode["trading"], mode["broker"]) == (trading, "ibkr")
    assert gateway in mode["detail"]
    [book] = client.get("/api/portfolios", headers=AUTH).json()["items"]
    assert book["trading"] == trading


def test_create_and_rename_your_own_portfolio(client, settings, people):
    alice = people["alice"]["headers"]
    made = client.post(
        "/api/portfolios", json={"name": "  Swing book ", "initial_cash": 25_000}, headers=alice
    )
    assert made.status_code == 201, made.text
    body = made.json()
    assert (body["name"], body["kind"], body["trading"]) == ("Swing book", "simulated", "paper")
    assert body["initial_cash"] == 25_000
    listed = client.get("/api/portfolios", headers=alice).json()["items"]
    assert [p["id"] for p in listed] == [body["id"]]

    renamed = client.patch(f"/api/portfolios/{body['id']}", json={"name": "Swing"}, headers=alice)
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["name"] == "Swing"
    with SqliteState(settings.state.path) as state:
        actions = [r["action"] for r in state.sql("SELECT action FROM audit_log ORDER BY id")]
    assert actions[-2:] == ["portfolio.create", "portfolio.rename"]


def test_portfolio_writes_are_scoped_and_checked(client, settings, people):
    pf = _portfolio(settings, people["alice"], "Alice book")
    bob, vic = people["bob"]["headers"], people["vic"]["headers"]
    assert client.patch(f"/api/portfolios/{pf}", json={"name": "x"}, headers=bob).status_code == 404
    assert client.post("/api/portfolios", json={"name": "v"}, headers=vic).status_code == 403
    alice = people["alice"]["headers"]
    assert client.post("/api/portfolios", json={"name": "  "}, headers=alice).status_code == 422
    negative = {"name": "x", "initial_cash": -1}
    assert client.post("/api/portfolios", json=negative, headers=alice).status_code == 422


# ---- subscriptions ---------------------------------------------------------------


def test_subscribe_list_and_scope(client, settings, people):
    alice, bob = people["alice"], people["bob"]
    pf = _portfolio(settings, alice, "Book")
    created = client.post(
        "/api/subscriptions",
        json={"strategy_id": "bah_active", "portfolio_id": pf, "mode": "paper"},
        headers=alice["headers"],
    )
    assert created.status_code == 201, created.text
    sub = created.json()
    assert sub["mode"] == "paper" and sub["enabled"] is True
    assert sub["strategy_status"] == "active"
    assert sub["paper_days_completed"] == 0 and sub["paper_days_required"] == 20
    assert any("paper trading days" in b for b in sub["auto_blockers"])
    assert sub["paused_reason"] is None

    listed = client.get("/api/subscriptions", headers=alice["headers"])
    assert [s["id"] for s in listed.json()["items"]] == [sub["id"]]
    assert client.get("/api/subscriptions", headers=bob["headers"]).json()["items"] == []

    # Bob can't subscribe Alice's portfolio, nor see or change her subscription.
    stolen = client.post(
        "/api/subscriptions",
        json={"strategy_id": "bah_active", "portfolio_id": pf, "mode": "paper"},
        headers=bob["headers"],
    )
    assert stolen.status_code == 404
    patched = client.patch(
        f"/api/subscriptions/{sub['id']}", json={"enabled": False}, headers=bob["headers"]
    )
    assert patched.status_code == 404


def test_subscribe_refusals(client, settings, people):
    alice = people["alice"]
    pf = _portfolio(settings, alice, "Book")
    viewer = client.post(
        "/api/subscriptions",
        json={"strategy_id": "bah_active", "mode": "notify"},
        headers=people["vic"]["headers"],
    )
    assert viewer.status_code == 403
    unknown = client.post(
        "/api/subscriptions",
        json={"strategy_id": "nope", "mode": "notify"},
        headers=alice["headers"],
    )
    assert unknown.status_code == 404
    no_book = client.post(
        "/api/subscriptions",
        json={"strategy_id": "bah_active", "mode": "paper"},
        headers=alice["headers"],
    )
    assert no_book.status_code == 422
    auto = client.post(
        "/api/subscriptions",
        json={"strategy_id": "bah_active", "portfolio_id": pf, "mode": "auto"},
        headers=alice["headers"],
    )
    assert auto.status_code == 409 and auto.json()["code"] == "auto_blocked"
    assert auto.json()["blockers"]


def test_a_second_follow_on_the_same_portfolio_is_a_conflict_not_a_crash(client, settings):
    """Approving a strategy makes the default portfolio follow it in Paper.
    Following it again there (in Alerts only, say) names the follow to
    change instead of failing with a server error."""
    body = {"strategy_id": "bah_active", "portfolio_id": DEFAULT_PORTFOLIO_ID, "mode": "notify"}
    again = client.post("/api/subscriptions", json=body, headers=AUTH)
    assert again.status_code == 409, again.text
    assert "already follows" in again.json()["detail"]
    alerts = client.post(
        "/api/subscriptions", json={"strategy_id": "bah_active", "mode": "notify"}, headers=AUTH
    )
    assert alerts.status_code == 201, alerts.text
    twice = client.post(
        "/api/subscriptions", json={"strategy_id": "bah_active", "mode": "notify"}, headers=AUTH
    )
    assert twice.status_code == 409, twice.text


def test_disable_enable_and_notify(client, settings, people):
    alice = people["alice"]
    pf = _portfolio(settings, alice, "Book")
    sub = client.post(
        "/api/subscriptions",
        json={"strategy_id": "bah_active", "portfolio_id": pf, "mode": "paper"},
        headers=alice["headers"],
    ).json()
    url = f"/api/subscriptions/{sub['id']}"
    off = client.patch(url, json={"enabled": False, "reason": "away"}, headers=alice["headers"])
    assert off.status_code == 200 and off.json()["enabled"] is False
    on = client.patch(url, json={"enabled": True}, headers=alice["headers"])
    assert on.json()["enabled"] is True
    notify = client.patch(url, json={"mode": "notify"}, headers=alice["headers"])
    assert notify.json()["mode"] == "notify"


def test_auto_needs_a_step_up_then_the_paper_record(app, client, settings, people):
    alice = people["alice"]
    pf = _portfolio(settings, alice, "Live", kind="broker")
    with SqliteState(settings.state.path) as state:
        link_connection(state, pf)
    sub = client.post(
        "/api/subscriptions",
        json={"strategy_id": "bah_active", "portfolio_id": pf, "mode": "paper"},
        headers=alice["headers"],
    ).json()
    url = f"/api/subscriptions/{sub['id']}"
    token = client.patch(url, json={"mode": "auto"}, headers=alice["headers"])
    assert token.status_code == 403 and token.json()["code"] == "step_up_required"

    allow_step_up(app)
    blocked = client.patch(url, json={"mode": "auto"}, headers=alice["headers"])
    assert blocked.status_code == 409, blocked.text
    body = blocked.json()
    assert body["code"] == "auto_blocked"
    assert body["blockers"] == ["0 of 20 paper trading days completed without a risk breach"]

    with SqliteState(settings.state.path) as state:
        seed_paper_days(state, sub["id"], 20, portfolio_id=pf)
    ok = client.patch(url, json={"mode": "auto", "reason": "ready"}, headers=alice["headers"])
    assert ok.status_code == 200, ok.text
    assert ok.json()["mode"] == "auto" and ok.json()["auto_blockers"] == []


def test_auto_replays_recent_sessions_before_it_starts(app, client, settings, people):
    """Roadmap 23.15: a strategy with no decision over the last sessions
    may not start an auto book, whatever its P&L."""
    alice = people["alice"]
    pf = _portfolio(settings, alice, "Live", kind="broker")
    with SqliteState(settings.state.path) as state:
        link_connection(state, pf)
    sub = client.post(
        "/api/subscriptions",
        json={"strategy_id": "bah_active", "portfolio_id": pf, "mode": "paper"},
        headers=alice["headers"],
    ).json()
    with SqliteState(settings.state.path) as state:
        seed_paper_days(state, sub["id"], 20, portfolio_id=pf)
    allow_step_up(app)
    url = f"/api/subscriptions/{sub['id']}"
    settings.production.universe = ["DOWN.US"]  # BuyAndHold on UP.US has no view here
    blocked = client.patch(url, json={"mode": "auto"}, headers=alice["headers"])
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["blockers"][0].startswith("recent replay: bah_active: no decision")
    settings.production.universe = ["UP.US"]
    ok = client.patch(url, json={"mode": "auto"}, headers=alice["headers"])
    assert ok.status_code == 200, ok.text


def _auto_subscription(app, client, settings, alice) -> tuple[str, str]:
    pf = _portfolio(settings, alice, "Live", kind="broker")
    with SqliteState(settings.state.path) as state:
        link_connection(state, pf)
    sub = client.post(
        "/api/subscriptions",
        json={"strategy_id": "bah_active", "portfolio_id": pf, "mode": "paper"},
        headers=alice["headers"],
    ).json()
    with SqliteState(settings.state.path) as state:
        seed_paper_days(state, sub["id"], 20, portfolio_id=pf)
    allow_step_up(app)
    url = f"/api/subscriptions/{sub['id']}"
    assert client.patch(url, json={"mode": "auto"}, headers=alice["headers"]).status_code == 200
    app.dependency_overrides.clear()
    return url, pf


@pytest.mark.parametrize("state_before", ["disabled", "paused"])
def test_ux02_turning_an_auto_subscription_back_on_needs_a_step_up(
    app, client, settings, people, state_before
):
    alice = people["alice"]
    url, _ = _auto_subscription(app, client, settings, alice)
    sub_id = url.rsplit("/", 1)[1]
    with SqliteState(settings.state.path) as state:
        if state_before == "disabled":
            state.execute("UPDATE subscriptions SET enabled = 0 WHERE id = ?", [sub_id])
        else:
            state.execute(
                "UPDATE subscriptions SET paused_reason = 'broker_error: x' WHERE id = ?", [sub_id]
            )
    token = client.patch(url, json={"enabled": True}, headers=alice["headers"])
    assert token.status_code == 403 and token.json()["code"] == "step_up_required"
    with SqliteState(settings.state.path) as state:
        row = state.sql("SELECT enabled, paused_reason FROM subscriptions WHERE id = ?", [sub_id])
    # nothing changed
    if state_before == "disabled":
        assert not row[0]["enabled"]
    else:
        assert row[0]["paused_reason"] == "broker_error: x"

    allow_step_up(app)
    on = client.patch(url, json={"enabled": True}, headers=alice["headers"])
    assert on.status_code == 200, on.text
    assert on.json()["enabled"] is True and on.json()["paused_reason"] is None


def test_ux02_turning_auto_back_on_runs_the_checklist(app, client, settings, people):
    alice = people["alice"]
    url, _ = _auto_subscription(app, client, settings, alice)
    sub_id = url.rsplit("/", 1)[1]
    with SqliteState(settings.state.path) as state:
        state.execute("UPDATE subscriptions SET enabled = 0 WHERE id = ?", [sub_id])
        state.execute("UPDATE broker_connections SET status = 'error'")
    allow_step_up(app)
    refused = client.patch(url, json={"enabled": True}, headers=alice["headers"])
    assert refused.status_code == 409 and refused.json()["code"] == "auto_blocked"
    with SqliteState(settings.state.path) as state:
        assert state.sql("SELECT enabled FROM subscriptions WHERE id = ?", [sub_id])[0][0] == 0


def test_ux02_turning_auto_off_needs_no_step_up(app, client, settings, people):
    alice = people["alice"]
    url, _ = _auto_subscription(app, client, settings, alice)
    off = client.patch(url, json={"enabled": False}, headers=alice["headers"])
    assert off.status_code == 200 and off.json()["enabled"] is False


# ---- BE-41: weights ------------------------------------------------------------------


@pytest.mark.parametrize("weight", ["Infinity", "NaN", "1e308", "101"])
def test_be41_a_weight_must_be_finite_and_bounded(client, settings, people, weight):
    alice = people["alice"]
    pf = _portfolio(settings, alice, "Book")
    body = f'{{"strategy_id": "bah_active", "portfolio_id": "{pf}", "mode": "paper", "weight": {weight}}}'
    made = client.post(
        "/api/subscriptions",
        content=body,
        headers={**alice["headers"], "Content-Type": "application/json"},
    )
    assert made.status_code == 422, made.text


def test_be41_a_book_whose_weights_sum_to_zero_is_refused(client, settings, people):
    alice = people["alice"]
    pf = _portfolio(settings, alice, "Book")
    zero = client.post(
        "/api/subscriptions",
        json={"strategy_id": "bah_active", "portfolio_id": pf, "mode": "paper", "weight": 0},
        headers=alice["headers"],
    )
    assert zero.status_code == 422, zero.text


# ---- 19.8: approve mode --------------------------------------------------------------


def test_approve_needs_a_step_up_and_the_auto_checklist(app, client, settings, people):
    alice = people["alice"]
    pf = _portfolio(settings, alice, "Live", kind="broker")
    with SqliteState(settings.state.path) as state:
        link_connection(state, pf)
    sub = client.post(
        "/api/subscriptions",
        json={"strategy_id": "bah_active", "portfolio_id": pf, "mode": "paper"},
        headers=alice["headers"],
    ).json()
    url = f"/api/subscriptions/{sub['id']}"
    token = client.patch(url, json={"mode": "approve"}, headers=alice["headers"])
    assert token.status_code == 403 and token.json()["code"] == "step_up_required"

    allow_step_up(app)
    blocked = client.patch(url, json={"mode": "approve"}, headers=alice["headers"])
    assert blocked.status_code == 409 and blocked.json()["code"] == "auto_blocked"

    with SqliteState(settings.state.path) as state:
        seed_paper_days(state, sub["id"], 20, portfolio_id=pf)
    ok = client.patch(url, json={"mode": "approve"}, headers=alice["headers"])
    assert ok.status_code == 200, ok.text
    assert ok.json()["mode"] == "approve" and ok.json()["auto_blockers"] == []

    # approve to auto takes the person out of each order: a fresh step-up again
    app.dependency_overrides.clear()
    again = client.patch(url, json={"mode": "auto"}, headers=alice["headers"])
    assert again.status_code == 403 and again.json()["code"] == "step_up_required"
    # back to paper needs no step-up
    paper = client.patch(url, json={"mode": "paper"}, headers=alice["headers"])
    assert paper.status_code == 200 and paper.json()["mode"] == "paper"


def test_a_subscription_cannot_start_in_approve(client, settings, people):
    alice = people["alice"]
    pf = _portfolio(settings, alice, "Live", kind="broker")
    made = client.post(
        "/api/subscriptions",
        json={"strategy_id": "bah_active", "portfolio_id": pf, "mode": "approve"},
        headers=alice["headers"],
    )
    assert made.status_code == 409, made.text

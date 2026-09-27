"""A live portfolio's allocation and account profile over the API (roadmap
19.6, 19.7): your own portfolios only, changes need a fresh second factor
and are audited."""

from __future__ import annotations

from stonks.accounts import PortfolioRepository, Scope
from stonks.store.state import SqliteState
from tests.integration.app.stepup import allow_step_up


def _portfolio(settings, person: dict, name: str) -> str:
    with SqliteState(settings.state.path) as state:
        scope = Scope(user_id=person["id"], role=person["role"])
        return PortfolioRepository(state).create(scope, name=name, kind="broker").id


def test_allocation_starts_empty_and_needs_a_step_up(app, client, settings, people):
    alice = people["alice"]["headers"]
    pid = _portfolio(settings, people["alice"], "Live")
    url = f"/api/portfolios/{pid}/live/allocation"
    got = client.get(url, headers=alice)
    assert got.status_code == 200 and got.json()["amount"] is None
    body = {"amount": 2500, "currency": "usd", "reason": "first slice"}
    refused = client.put(url, json=body, headers=alice)
    assert refused.status_code == 403 and refused.json()["code"] == "step_up_required"

    allow_step_up(app)
    ok = client.put(url, json=body, headers=alice)
    assert ok.status_code == 200, ok.text
    view = ok.json()
    assert (view["amount"], view["currency"], view["updated_by"]) == (
        2500.0,
        "USD",
        f"user:{people['alice']['id']}",
    )
    assert client.put(url, json={**body, "amount": -1}, headers=alice).status_code == 422
    with SqliteState(settings.state.path) as state:
        rows = state.sql("SELECT actor FROM audit_log WHERE action = 'live.allocation_set'")
    assert [r["actor"] for r in rows] == [f"user:{people['alice']['id']}"]


def test_another_persons_portfolio_reads_as_missing(app, client, settings, people):
    pid = _portfolio(settings, people["alice"], "Live")
    allow_step_up(app)
    for who in ("bob", "ada"):
        headers = people[who]["headers"]
        assert (
            client.get(f"/api/portfolios/{pid}/live/allocation", headers=headers).status_code == 404
        )
        put = client.put(
            f"/api/portfolios/{pid}/live/allocation",
            json={"amount": 1, "currency": "USD", "reason": "x"},
            headers=headers,
        )
        assert put.status_code == 404
    viewer = client.put(
        f"/api/portfolios/{pid}/live/allocation",
        json={"amount": 1, "currency": "USD", "reason": "x"},
        headers=people["vic"]["headers"],
    )
    assert viewer.status_code == 403


def test_account_profile_round_trip(app, client, settings, people):
    alice = people["alice"]["headers"]
    pid = _portfolio(settings, people["alice"], "Live")
    url = f"/api/portfolios/{pid}/live/account-profile"
    assert client.get(url, headers=alice).status_code == 404
    allow_step_up(app)
    short_on_cash = {"jurisdiction": "us", "account_type": "cash", "allow_short": True}
    assert client.put(url, json=short_on_cash, headers=alice).status_code == 422
    ok = client.put(url, json={"jurisdiction": "uk", "base_currency": "gbp"}, headers=alice)
    assert ok.status_code == 200, ok.text
    assert ok.json() == {
        "portfolio_id": pid,
        "jurisdiction": "uk",
        "account_type": "cash",
        "client_class": "retail",
        "base_currency": "GBP",
        "fx_policy": "refuse",
        "wash_sale_mode": "warn",
        "allow_short": False,
    }
    assert client.get(url, headers=alice).json()["jurisdiction"] == "uk"


def _rules_on(settings) -> None:
    from stonks.production.rules._account_settings import AccountRulesSettings
    from stonks.production.rules._stop_settings import ProtectiveStopSettings
    from stonks.production.rules.capital_ramp import CapitalRampSettings
    from stonks.production.rules.settings import RuleSettings

    rules = RuleSettings(
        capital_ramp=CapitalRampSettings(enabled=True),
        account_rules=AccountRulesSettings(enabled=True),
        protective_stops=ProtectiveStopSettings(enabled=True, atr_multiple=2.5),
    )
    settings.production.risk = settings.production.risk.model_copy(update={"rules": rules})


def test_live_rules_show_what_is_on_and_what_applies(app, client, settings, people):
    alice = people["alice"]["headers"]
    pid = _portfolio(settings, people["alice"], "Live")
    _rules_on(settings)
    got = client.get(f"/api/portfolios/{pid}/live/rules", headers=alice)
    assert got.status_code == 200, got.text
    view = got.json()
    on = {r["name"]: r["on"] for r in view["safeguards"]}
    assert on == {
        "capital_ramp": True,
        "live_notional_caps": False,
        "price_band": False,
        "account_rules": True,
        "max_orders_per_run": False,
        "stop_cooldown": False,
        "stop_guard": False,
        "losing_lock": False,
        "protective_stops": True,
    }
    stops = next(r for r in view["safeguards"] if r["name"] == "protective_stops")
    assert stops["settings"]["atr_multiple"] == 2.5
    assert view["account_rules_on"] is True and view["profile_set"] is False
    assert not any(r["applies"] for r in view["account_rules"])

    allow_step_up(app)
    profile = {"jurisdiction": "us", "account_type": "cash"}
    put = client.put(f"/api/portfolios/{pid}/live/account-profile", json=profile, headers=alice)
    assert put.status_code == 200
    view = client.get(f"/api/portfolios/{pid}/live/rules", headers=alice).json()
    applies = {r["name"] for r in view["account_rules"] if r["applies"]}
    assert view["profile_set"] is True
    assert {"settled_cash", "wash_sale", "reg_sho", "restricted"} <= applies
    assert not applies & {"buying_power", "pdt", "priips_kid", "short_disclosure"}


def test_live_rules_of_another_person_read_as_missing(client, settings, people):
    pid = _portfolio(settings, people["alice"], "Live")
    for who in ("bob", "ada"):
        got = client.get(f"/api/portfolios/{pid}/live/rules", headers=people[who]["headers"])
        assert got.status_code == 404


def _gateways(settings, portfolios: list[str]) -> None:
    from stonks.execution.brokers.ibkr.settings import IbkrBrokerConfig

    settings.brokers.ibkr = IbkrBrokerConfig.model_validate(
        {
            "gateways": {
                "live": {
                    "host": "ibkr-live",
                    "port": 4003,
                    "mode": "live",
                    "portfolios": portfolios,
                },
                "paper": {"host": "ibkr-paper", "port": 4004, "mode": "paper"},
            }
        }
    )


def test_gateway_health_shows_your_paused_books_only(client, settings, people):
    mine = _portfolio(settings, people["alice"], "Alice live")
    theirs = _portfolio(settings, people["bob"], "Bob live")
    _gateways(settings, [mine, theirs])
    with SqliteState(settings.state.path) as state:
        state.execute(
            "INSERT INTO broker_gateway_status (gateway, mode, connected, last_check_at,"
            " last_ok_at, down_since, consecutive_failures, detail, paused_at)"
            " VALUES ('live', 'live', 0, '2026-09-27T14:00:00+00:00',"
            " '2026-09-25T14:00:00+00:00', '2026-09-25T14:05:00+00:00', 12,"
            " 'ConnectionRefusedError', '2026-09-27T14:00:00+00:00')"
        )
        for sid, pid in (("sub_a", mine), ("sub_b", theirs)):
            state.execute(
                "INSERT INTO subscriptions (id, user_id, strategy_id, portfolio_id, mode,"
                " paused_reason, created_at, updated_at)"
                " SELECT ?, owner_id, 'bah_active', id, 'auto', 'broker error: gateway down',"
                " '2026-09-01', '2026-09-01' FROM portfolios WHERE id = ?",
                [sid, pid],
            )
    got = client.get("/api/brokers/gateways", headers=people["alice"]["headers"])
    assert got.status_code == 200, got.text
    view = got.json()
    assert view["configured"] is True
    live, paper = view["gateways"]
    assert (live["gateway"], live["connected"], live["checked"]) == ("live", False, True)
    assert live["last_ok_at"] == "2026-09-25T14:00:00+00:00"
    assert live["your_portfolios"] == ["Alice live"]
    assert [b["subscription_id"] for b in live["paused_books"]] == ["sub_a"]
    assert live["paused_elsewhere"] == 1
    assert (paper["gateway"], paper["checked"], paper["connected"]) == ("paper", False, False)
    ada = client.get("/api/brokers/gateways", headers=people["ada"]["headers"]).json()
    assert ada["gateways"][0]["paused_books"] == []
    assert ada["gateways"][0]["paused_elsewhere"] == 2


def test_gateway_health_is_empty_without_gateways(client, people):
    got = client.get("/api/brokers/gateways", headers=people["vic"]["headers"])
    assert got.status_code == 200
    assert got.json() == {"configured": False, "gateways": []}


# ---- stages, gates and the preview (19.9) --------------------------------------------


def _move(settings, pid: str, *stages: str) -> None:
    from stonks.production.live.stages import change_stage

    with SqliteState(settings.state.path) as state:
        for stage in stages:
            change_stage(
                state, pid, stage, actor="t", reason="setup",
                gate_report={"target": stage, "passed": True},
            )  # fmt: skip


def _ready_for_broker_paper(settings, pid: str) -> None:
    with SqliteState(settings.state.path) as state:
        state.execute(
            "INSERT INTO subscriptions (id, user_id, strategy_id, portfolio_id, mode,"
            " paper_days_completed, created_at, updated_at)"
            " SELECT 'sub_live', owner_id, 'bah_active', id, 'paper', 20, 'x', 'x'"
            " FROM portfolios WHERE id = ?",
            [pid],
        )


def test_stage_starts_in_sim_paper_and_promotion_needs_a_step_up(app, client, settings, people):
    alice = people["alice"]["headers"]
    pid = _portfolio(settings, people["alice"], "Live")
    got = client.get(f"/api/portfolios/{pid}/live/stage", headers=alice)
    assert got.status_code == 200, got.text
    view = got.json()
    assert (view["stage"], view["next_stage"], view["real_money"]) == (
        "sim_paper",
        "broker_paper",
        False,
    )
    assert view["history"] == [] and view["days"] == []
    url = f"/api/portfolios/{pid}/live/stage/promote"
    body = {"to_stage": "broker_paper", "reason": "soak", "confirm": "broker_paper"}
    refused = client.post(url, json=body, headers=alice)
    assert refused.status_code == 403 and refused.json()["code"] == "step_up_required"

    allow_step_up(app)
    report = client.get(f"/api/portfolios/{pid}/live/gate-report", headers=alice).json()
    assert report["target"] == "broker_paper" and report["passed"] is False
    failed = client.post(url, json=body, headers=alice)
    assert failed.status_code == 409 and "paper_days" in failed.json()["detail"]
    wrong = client.post(url, json={**body, "confirm": "yes"}, headers=alice)
    assert wrong.status_code == 422

    _ready_for_broker_paper(settings, pid)
    ok = client.post(url, json=body, headers=alice)
    assert ok.status_code == 200, ok.text
    view = ok.json()
    assert view["stage"] == "broker_paper"
    [change] = view["history"]
    assert change["direction"] == "promote" and change["gate_report"]["passed"] is True
    skip = {"to_stage": "live_scale", "reason": "x", "confirm": "live_scale"}
    assert client.post(url, json=skip, headers=alice).status_code == 409


def test_demotion_needs_no_step_up_and_only_goes_down(client, settings, people):
    alice = people["alice"]["headers"]
    pid = _portfolio(settings, people["alice"], "Live")
    url = f"/api/portfolios/{pid}/live/stage/demote"
    same = client.post(url, json={"to_stage": "sim_paper", "reason": "x"}, headers=alice)
    assert same.status_code == 409
    _move(settings, pid, "broker_paper", "live_small")
    ok = client.post(url, json={"to_stage": "sim_paper", "reason": "bad week"}, headers=alice)
    assert ok.status_code == 200, ok.text
    assert ok.json()["stage"] == "sim_paper"
    assert ok.json()["history"][0]["direction"] == "demote"
    viewer = client.post(
        url, json={"to_stage": "sim_paper", "reason": "x"}, headers=people["vic"]["headers"]
    )
    assert viewer.status_code == 403
    other = client.get(f"/api/portfolios/{pid}/live/stage", headers=people["bob"]["headers"])
    assert other.status_code == 404


def test_the_account_profile_is_locked_while_trading_real_money(app, client, settings, people):
    alice = people["alice"]["headers"]
    pid = _portfolio(settings, people["alice"], "Live")
    allow_step_up(app)
    url = f"/api/portfolios/{pid}/live/account-profile"
    assert client.put(url, json={"jurisdiction": "us"}, headers=alice).status_code == 200
    _move(settings, pid, "broker_paper", "live_small")
    locked = client.put(url, json={"jurisdiction": "uk"}, headers=alice)
    assert locked.status_code == 409 and "locked" in locked.json()["detail"]


def test_a_portfolio_without_a_live_book_has_nothing_to_preview(client, settings, people):
    alice = people["alice"]["headers"]
    pid = _portfolio(settings, people["alice"], "Live")
    got = client.post(f"/api/portfolios/{pid}/live/preview", headers=alice)
    assert got.status_code == 422 and "no live book" in got.json()["detail"]
    viewer = client.post(f"/api/portfolios/{pid}/live/preview", headers=people["vic"]["headers"])
    assert viewer.status_code == 403

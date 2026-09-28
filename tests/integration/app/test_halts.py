"""The kill switch and risk halts through the service layer and the REST
API (roadmap 12.6, BL-28): scopes, ownership, the audit trail and the typed
confirmation to resume."""

from __future__ import annotations

import json

import pytest

from stonks.accounts import DEFAULT_OWNER_ID, PortfolioRepository, Role, Scope, UserRepository
from stonks.api.deps import current_principal
from stonks.app.errors import NotFoundError, ValidationError
from stonks.app.halts import (
    RESUME_PHRASE,
    ClearHaltRequest,
    HaltService,
    KillSwitchRequest,
    ResumeRequest,
)
from stonks.auth import PermissionDenied, Principal, StepUpRequired
from stonks.auth.principal import ROLE_SCOPES
from stonks.production.halts import trip_halt
from stonks.store.state import SqliteState
from tests.integration.app.test_api import AUTH


@pytest.fixture
def halts(services) -> HaltService:
    return HaltService(services.context)


@pytest.fixture
def people(settings, services):
    with SqliteState(settings.state.path) as state:
        users = UserRepository(state)
        owner = Scope.for_user(users.get(DEFAULT_OWNER_ID))
        trader = Scope.for_user(users.create(display_name="T", role=Role.TRADER, actor="t"))
        viewer = Scope.for_user(users.create(display_name="V", role=Role.VIEWER, actor="t"))
        pf = PortfolioRepository(state).create(trader, name="T book").id
    return {"owner": owner, "trader": trader, "viewer": viewer, "trader_pf": pf}


def _audit(settings, action):
    with SqliteState(settings.state.path) as state:
        return [
            dict(r)
            for r in state.sql("SELECT * FROM audit_log WHERE action = ? ORDER BY id", [action])
        ]


def test_an_admin_engages_the_global_kill_switch_and_it_is_audited(halts, people, settings):
    view = halts.engage_kill(people["owner"], KillSwitchRequest(scope="global", reason="panic"))
    assert (view.kind, view.scope, view.halt, view.active) == ("kill", "global", "all", True)
    [row] = _audit(settings, "kill_switch.engage")
    assert row["actor"] == f"user:{DEFAULT_OWNER_ID}"
    assert json.loads(row["details_json"])["scope"] == "global"
    # engaging again returns the open one
    again = halts.engage_kill(people["owner"], KillSwitchRequest(scope="global", reason="x"))
    assert again.id == view.id


def test_only_an_admin_can_stop_everyone(halts, people):
    with pytest.raises(PermissionDenied, match="admin"):
        halts.engage_kill(people["trader"], KillSwitchRequest(scope="global", reason="r"))


def test_a_viewer_cannot_use_the_kill_switch(halts, people):
    with pytest.raises(PermissionDenied, match="trade"):
        halts.engage_kill(people["viewer"], KillSwitchRequest(scope="user", reason="r"))


def test_a_trader_stops_their_own_books_and_stops_buys_on_one(halts, people):
    trader, pf = people["trader"], people["trader_pf"]
    mine = halts.engage_kill(trader, KillSwitchRequest(scope="user", reason="away"))
    assert mine.user_id == trader.user_id and mine.halt == "all"
    flat = halts.engage_kill(
        trader, KillSwitchRequest(scope="portfolio", portfolio_id=pf, buys_only=True, reason="exit")
    )
    assert flat.portfolio_id == pf and flat.halt == "buys"


def test_another_users_portfolio_reads_as_missing(halts, people):
    with pytest.raises(NotFoundError):
        halts.engage_kill(
            people["owner"],
            KillSwitchRequest(scope="portfolio", portfolio_id=people["trader_pf"], reason="r"),
        )


def test_resume_needs_the_typed_confirmation(halts, people, settings):
    view = halts.engage_kill(people["owner"], KillSwitchRequest(scope="global", reason="panic"))
    with pytest.raises(ValidationError, match=RESUME_PHRASE):
        halts.resume_kill(people["owner"], view.id, ResumeRequest(confirmation="yes", reason="ok"))
    resumed = halts.resume_kill(
        people["owner"], view.id, ResumeRequest(confirmation=RESUME_PHRASE, reason="calm again")
    )
    assert not resumed.active and resumed.cleared_by == f"user:{DEFAULT_OWNER_ID}"
    assert len(_audit(settings, "kill_switch.resume")) == 1
    with SqliteState(settings.state.path) as state:
        resets = state.sql("SELECT COUNT(*) FROM status_changes WHERE kind = 'risk_reset'")
    assert resets[0][0] == 1


def test_a_trader_cannot_resume_the_global_switch(halts, people):
    view = halts.engage_kill(people["owner"], KillSwitchRequest(scope="global", reason="panic"))
    with pytest.raises(PermissionDenied, match="admin"):
        halts.resume_kill(
            people["trader"], view.id, ResumeRequest(confirmation=RESUME_PHRASE, reason="r")
        )


def test_clearing_a_breaker_halt_is_the_owners_logged_reset(halts, people, settings):
    with SqliteState(settings.state.path) as state:
        halt, _ = trip_halt(
            state, "drawdown", reason="dd", actor="system", portfolio_id=people["trader_pf"]
        )
    with pytest.raises(NotFoundError):
        halts.clear(people["owner"], halt.id, ClearHaltRequest(reason="not mine"))
    with pytest.raises(ValidationError, match="resume"):
        kill = halts.engage_kill(people["trader"], KillSwitchRequest(scope="user", reason="r"))
        halts.clear(people["trader"], kill.id, ClearHaltRequest(reason="r"))
    cleared = halts.clear(people["trader"], halt.id, ClearHaltRequest(reason="reviewed"))
    assert not cleared.active
    assert len(_audit(settings, "risk_halt.clear")) == 1


def test_each_user_sees_global_halts_and_their_own(halts, people):
    halts.engage_kill(people["owner"], KillSwitchRequest(scope="user", reason="owner"))
    halts.engage_kill(people["trader"], KillSwitchRequest(scope="user", reason="trader"))
    assert [v.reason for v in halts.list(people["trader"])] == ["trader"]
    halts.engage_kill(people["owner"], KillSwitchRequest(scope="global", reason="all"))
    assert sorted(v.reason for v in halts.list(people["trader"])) == ["all", "trader"]


# ---- REST ----------------------------------------------------------------------------


def test_the_rest_routes_engage_list_and_resume(client):
    r = client.post("/api/halts/kill", json={"scope": "global", "reason": "panic"}, headers=AUTH)
    assert r.status_code == 201, r.text
    halt_id = r.json()["id"]
    listed = client.get("/api/halts", headers=AUTH).json()["items"]
    assert [h["id"] for h in listed] == [halt_id]
    bad = client.post(
        f"/api/halts/{halt_id}/resume",
        json={"confirmation": "resume", "reason": "ok"},
        headers=AUTH,
    )
    # The legacy token can't resume: resuming needs a fresh second factor.
    assert bad.status_code == 403 and "step_up_required" in bad.json()["detail"]
    owner = Scope(user_id=DEFAULT_OWNER_ID, role=Role.ADMIN)
    client.app.dependency_overrides[current_principal] = lambda: _principal(
        owner, via="session", fresh=True
    )
    try:
        bad = client.post(
            f"/api/halts/{halt_id}/resume",
            json={"confirmation": "resume", "reason": "ok"},
            headers=AUTH,
        )
        assert bad.status_code == 422
        ok = client.post(
            f"/api/halts/{halt_id}/resume",
            json={"confirmation": RESUME_PHRASE, "reason": "ok"},
            headers=AUTH,
        )
    finally:
        client.app.dependency_overrides.clear()
    assert ok.status_code == 200 and ok.json()["active"] is False
    assert client.get("/api/halts", headers=AUTH).json()["items"] == []
    assert client.get("/api/halts?include_cleared=true", headers=AUTH).json()["total"] == 1


def test_the_rest_routes_need_the_token(client):
    assert client.get("/api/halts").status_code == 401
    assert client.post("/api/halts/kill", json={"scope": "user", "reason": "r"}).status_code == 401


# ---- step-up to resume (design section 2) -----------------------------------------


def _principal(scope: Scope, *, via: str, fresh: bool) -> Principal:
    return Principal.create(
        user_id=scope.user_id,
        kind=scope.kind,
        role=scope.role,
        scopes=ROLE_SCOPES[scope.role],
        mfa_fresh=fresh,
        via=via,
    )


def test_resume_needs_a_fresh_second_factor_for_sessions(halts, people):
    trader = people["trader"]
    halt = halts.engage_kill(trader, KillSwitchRequest(scope="user", reason="away"))
    body = ResumeRequest(confirmation=RESUME_PHRASE, reason="back")
    with pytest.raises(StepUpRequired):
        halts.resume_kill(_principal(trader, via="session", fresh=False), halt.id, body)
    with pytest.raises(StepUpRequired):
        halts.resume_kill(_principal(trader, via="token", fresh=False), halt.id, body)
    view = halts.resume_kill(_principal(trader, via="session", fresh=True), halt.id, body)
    assert view.active is False


def test_the_cli_scope_resumes_without_step_up(halts, people):
    # Shell access to the server implies admin (design section 2).
    halt = halts.engage_kill(people["owner"], KillSwitchRequest(scope="global", reason="x"))
    body = ResumeRequest(confirmation=RESUME_PHRASE, reason="back")
    assert halts.resume_kill(people["owner"], halt.id, body).active is False


def test_engage_and_list_accept_a_principal(halts, people):
    trader = people["trader"]
    p = _principal(trader, via="token", fresh=False)
    view = halts.engage_kill(p, KillSwitchRequest(scope="user", reason="away"))
    assert view.user_id == trader.user_id
    assert [h.id for h in halts.list(p)] == [view.id]


# ---- escalating the kill switch (TO-07) ------------------------------------------


def test_buys_only_escalates_to_stop_all(halts, people, settings):
    from datetime import UTC, datetime

    from stonks.production.halts import active_halts

    owner = people["owner"]
    flat = halts.engage_kill(
        owner, KillSwitchRequest(scope="user", buys_only=True, reason="exit slowly")
    )
    assert flat.halt == "buys"
    stop = halts.engage_kill(owner, KillSwitchRequest(scope="user", reason="fills look wrong"))
    assert stop.halt == "all" and stop.active
    with SqliteState(settings.state.path) as state:
        [open_halt] = active_halts(
            state, datetime.now(UTC).date(), portfolio_id="pf_default", user_id=DEFAULT_OWNER_ID
        )
    assert open_halt.id == stop.id and open_halt.halt == "all"
    [row] = _audit(settings, "kill_switch.escalate")
    assert json.loads(row["details_json"])["escalated_from"] == flat.id
    # the replaced row says why it closed, and nothing counts it as a reset
    old = halts.get(owner, flat.id)
    assert not old.active and old.clear_reason.startswith("escalated to all")


def test_the_escalated_halt_stops_sells_at_the_gate(halts, people, settings):
    from datetime import UTC, datetime

    from stonks.production.hooks import GateContext
    from stonks.production.hooks.risk_halts import RiskHaltGate

    trader, pf = people["trader"], people["trader_pf"]
    req = KillSwitchRequest(scope="portfolio", portfolio_id=pf, buys_only=True, reason="exit")
    halts.engage_kill(trader, req)
    halts.engage_kill(
        trader, KillSwitchRequest(scope="portfolio", portfolio_id=pf, reason="stop all")
    )
    with SqliteState(settings.state.path) as state:
        verdict = RiskHaltGate().check(
            GateContext(
                state=state,
                as_of=datetime.now(UTC).date(),
                portfolio_id=pf,
                owner_id=trader.user_id,
                dry_run=False,
                policy=None,
            )
        )
    assert verdict is not None and verdict.halt == "all"


def test_buys_only_never_downgrades_a_stop_all(halts, people):
    owner = people["owner"]
    stop = halts.engage_kill(owner, KillSwitchRequest(scope="global", reason="stop"))
    again = halts.engage_kill(
        owner, KillSwitchRequest(scope="global", buys_only=True, reason="flatten")
    )
    assert again.id == stop.id and again.halt == "all"


# ---- cancelling working broker orders (TO-08) ------------------------------------


def _pending_order(settings, client_id, *, side="buy", portfolio_id="pf_default"):
    with SqliteState(settings.state.path) as state:
        state.execute(
            "INSERT INTO orders (client_id, ticker, side, quantity, order_type, status,"
            " created_at, updated_at, portfolio_id)"
            " VALUES (?, 'UP.US', ?, 5, 'market', 'pending', 'x', 'x', ?)",
            [client_id, side, portfolio_id],
        )


def _order_status(settings, client_id):
    with SqliteState(settings.state.path) as state:
        return state.sql("SELECT status FROM orders WHERE client_id = ?", [client_id])[0][0]


@pytest.fixture
def broker():
    from tests.integration.test_cancel_working_orders import CancellingBroker

    return CancellingBroker()


@pytest.fixture
def live_halts(services, broker) -> HaltService:
    """pf_default trades at a (fake) external broker."""
    return HaltService(
        services.context, brokers=lambda pid: broker if pid == "pf_default" else None
    )


def test_the_global_kill_switch_cancels_working_broker_orders(live_halts, people, broker, settings):
    _pending_order(settings, "2026-03-20:bh:UP.US:buy")
    broker.set("2026-03-20:bh:UP.US:buy", "pending", 0.0, None)
    live_halts.engage_kill(people["owner"], KillSwitchRequest(scope="global", reason="bad tick"))
    assert broker.cancelled == ["2026-03-20:bh:UP.US:buy"]
    assert _order_status(settings, "2026-03-20:bh:UP.US:buy") == "cancelled"
    [row] = _audit(settings, "kill_switch.cancel_orders")
    assert json.loads(row["details_json"])["cancelled"] == ["2026-03-20:bh:UP.US:buy"]


def test_buys_only_cancels_working_buys_only(live_halts, people, broker, settings):
    _pending_order(settings, "b1")
    _pending_order(settings, "s1", side="sell")
    broker.set("b1", "pending", 0.0, None)
    broker.set("s1", "pending", 0.0, None, side="sell")
    live_halts.engage_kill(
        people["owner"], KillSwitchRequest(scope="user", buys_only=True, reason="exit")
    )
    assert broker.cancelled == ["b1"]
    assert _order_status(settings, "s1") == "pending"


def test_another_users_kill_switch_cancels_nothing_of_mine(live_halts, people, broker, settings):
    _pending_order(settings, "b1")
    broker.set("b1", "pending", 0.0, None)
    live_halts.engage_kill(people["trader"], KillSwitchRequest(scope="user", reason="mine"))
    assert broker.cancelled == []


def test_a_broker_failure_never_undoes_the_halt(services, people, settings):
    def broken(pid):
        raise RuntimeError("no keys")

    halts = HaltService(services.context, brokers=broken)
    view = halts.engage_kill(people["owner"], KillSwitchRequest(scope="global", reason="x"))
    assert view.active


def test_flatten_is_a_deprecated_alias_of_buys_only():
    old = KillSwitchRequest.model_validate({"scope": "user", "reason": "r", "flatten": True})
    assert old.buys_only is True
    assert KillSwitchRequest(scope="user", reason="r").buys_only is False


def test_the_api_takes_the_old_flatten_field(client):
    r = client.post(
        "/api/halts/kill", json={"scope": "user", "reason": "r", "flatten": True}, headers=AUTH
    )
    assert r.status_code == 201, r.text
    assert r.json()["halt"] == "buys"


# ---- IBKR: the kill switch while a tick holds the gateway (roadmap 19.17) ----------------

TICK_ORDER = "2026-09-28:t1:s1:AAPL.US:buy"


@pytest.fixture
def ibkr_tick():
    """A running tick at a fake IB Gateway: client 11 connected, one working
    buy it placed. The API builds its brokers on client 16."""
    from stonks.core.types import Order
    from stonks.execution.brokers.ibkr.factory import connect_ibkr
    from stonks.execution.brokers.ibkr.settings import IbkrBrokerConfig
    from tests.fakes.ib_gateway import FakeIbGateway

    gw = FakeIbGateway()
    config = IbkrBrokerConfig(
        gateways={"paper": {"host": "gw", "port": 1, "mode": "paper", "portfolios": ["pf_default"]}}
    )

    def build(role):
        return connect_ibkr(config, role=role, client_factory=lambda ep: gw.session(ep.client_id))

    tick = build("tick")
    tick.place_order(Order(client_id=TICK_ORDER, ticker="AAPL.US", side="buy", quantity=5.0,
                           order_type="limit", limit_price=1.0, time_in_force="day"))  # fmt: skip
    return gw, tick, build


def _ibkr_halts(services, build):
    return HaltService(
        services.context, brokers=lambda pid: build("api") if pid == "pf_default" else None
    )


def test_the_global_kill_switch_cancels_a_running_ticks_orders(services, people, settings,
                                                               ibkr_tick):  # fmt: skip
    gw, _tick, build = ibkr_tick
    _pending_order(settings, TICK_ORDER)
    _ibkr_halts(services, build).engage_kill(
        people["owner"], KillSwitchRequest(scope="global", reason="runaway")
    )
    # the single cancel needs client 11, which the tick holds: the global
    # cancel stops it anyway
    assert gw.global_cancels == 1
    assert gw.trade(TICK_ORDER).status == "Cancelled"
    assert _order_status(settings, TICK_ORDER) == "cancelled"
    [row] = _audit(settings, "kill_switch.cancel_orders")
    assert json.loads(row["details_json"]) == {"cancelled": [TICK_ORDER], "failed": []}
    # every API session was given back
    assert not any(s.connected for s in gw.sessions if s.client_id == 16)


def test_a_buys_only_kill_never_cancels_everything(services, people, settings, ibkr_tick):
    gw, _tick, build = ibkr_tick
    _pending_order(settings, TICK_ORDER)
    _ibkr_halts(services, build).engage_kill(
        people["owner"], KillSwitchRequest(scope="global", buys_only=True, reason="exit")
    )
    assert gw.global_cancels == 0
    assert _order_status(settings, TICK_ORDER) == "pending"
    [row] = _audit(settings, "kill_switch.cancel_orders")
    assert json.loads(row["details_json"])["failed"] == [TICK_ORDER]


def test_once_the_tick_is_done_the_kill_switch_cancels_one_by_one(services, people, settings,
                                                                  ibkr_tick):  # fmt: skip
    gw, tick, build = ibkr_tick
    tick.close()
    _pending_order(settings, TICK_ORDER)
    _ibkr_halts(services, build).engage_kill(
        people["owner"], KillSwitchRequest(scope="global", buys_only=True, reason="exit")
    )
    assert gw.global_cancels == 0
    assert gw.cancels_by == [(11, 1)]
    assert _order_status(settings, TICK_ORDER) == "cancelled"


def test_a_portfolio_kill_cancels_everything_only_when_it_covers_the_gateway(
    services, people, settings, ibkr_tick
):
    gw, _tick, build = ibkr_tick
    _pending_order(settings, TICK_ORDER)
    _ibkr_halts(services, build).engage_kill(
        people["owner"],
        KillSwitchRequest(scope="portfolio", portfolio_id="pf_default", reason="stop"),
    )
    # the gateway serves only pf_default, so cancelling all of it is safe
    assert gw.global_cancels == 1
    assert _order_status(settings, TICK_ORDER) == "cancelled"


# ---- resume checks (roadmap 23.15) --------------------------------------------------


class _Account:
    def __init__(self, equity):
        self.equity = equity


class CheckedBroker:
    """A broker the resume checks read: positions, account and quotes."""

    def __init__(self, equity=100_000.0, positions=None, down=False):
        from stonks.core.types import Portfolio

        self._portfolio = Portfolio(cash=equity, positions=positions or {})
        self._equity = equity
        self.down = down

    def fetch_portfolio(self):
        if self.down:
            from stonks.execution.brokers.base import BrokerUnavailableError

            raise BrokerUnavailableError("gateway down")
        return self._portfolio

    def fetch_account(self):
        return _Account(self._equity)

    def quotes(self, tickers):
        return {}


def _checked_halts(services, broker):
    return HaltService(
        services.context, brokers=lambda pid: broker if pid == "pf_default" else None
    )


def test_resume_checks_are_read_before_the_phrase(services, people):
    halts = _checked_halts(services, CheckedBroker())
    halt = halts.engage_kill(people["owner"], KillSwitchRequest(scope="global", reason="r"))
    view = halts.resume_checks(people["owner"], halt.id)
    assert view.passed
    names = {c.name for c in view.checks if c.portfolio_id == "pf_default"}
    assert {"gateway_up", "last_reconcile_clean", "account_readable", "equity_cover"} <= names


def test_a_failed_check_refuses_the_resume_unless_overridden(services, people, settings):
    from stonks.app.errors import ConflictError

    halts = _checked_halts(services, CheckedBroker(down=True))
    halt = halts.engage_kill(people["owner"], KillSwitchRequest(scope="global", reason="r"))
    assert not halts.resume_checks(people["owner"], halt.id).passed
    body = ResumeRequest(confirmation=RESUME_PHRASE, reason="back")
    with pytest.raises(ConflictError, match="gateway_up"):
        halts.resume_kill(people["owner"], halt.id, body)
    forced = ResumeRequest(confirmation=RESUME_PHRASE, reason="back", override_checks=True)
    assert halts.resume_kill(people["owner"], halt.id, forced).active is False
    [row] = _audit(settings, "kill_switch.resume")
    details = json.loads(row["details_json"])
    assert details["override_checks"] is True
    assert any(c["name"] == "gateway_up" and c["passed"] is False for c in details["checks"])


def test_the_resume_checks_route(client):
    halt_id = client.post(
        "/api/halts/kill", json={"scope": "global", "reason": "r"}, headers=AUTH
    ).json()["id"]
    got = client.get(f"/api/halts/{halt_id}/resume-checks", headers=AUTH)
    assert got.status_code == 200, got.text
    assert got.json()["passed"] is True and got.json()["halt_id"] == halt_id

"""Margin accounts over the API (roadmap 19.13): choosing a margin profile
needs margin accounts on, the account rules and the margin call rule on, a
fresh second factor, the risks acknowledged, and the broker reporting a
margin account. The margin view shows buying power and margin use."""

from __future__ import annotations

import pytest

from stonks.accounts import PortfolioRepository, Scope
from stonks.app.live import LiveService
from stonks.execution.brokers.base import LiveAccountState
from stonks.store.state import SqliteState
from tests.integration.app.stepup import allow_step_up

MARGIN = {
    "jurisdiction": "us",
    "account_type": "margin",
    "allow_short": True,
    "acknowledge_margin_risks": True,
}


class _Broker:
    def __init__(self, account: LiveAccountState | None, fail: bool = False) -> None:
        self.account = account
        self.fail = fail
        self.closed = 0

    def fetch_portfolio(self):  # pragma: no cover - not used
        raise NotImplementedError

    def place_order(self, order):  # pragma: no cover - not used
        raise NotImplementedError

    def reconcile(self):  # pragma: no cover - not used
        return []

    def fetch_account(self) -> LiveAccountState:
        if self.fail or self.account is None:
            raise RuntimeError("gateway down")
        return self.account

    def close(self) -> None:
        self.closed += 1


def account(reported="margin", *, equity=20_000.0, excess=6_000.0, **kw) -> LiveAccountState:
    base: dict[str, object] = {
        "equity": equity,
        "cash": 1_000.0,
        "settled_cash": 1_000.0,
        "available_funds": 5_000.0,
        "buying_power": 20_000.0,
        "currency": "USD",
        "account_type": "margin",
        "reported_type": reported,
        "excess_liquidity": excess,
        "initial_margin": 12_000.0,
        "maintenance_margin": 0.0 if excess is None else equity - excess,
        "day_trades_remaining": 2,
    }
    base.update(kw)
    return LiveAccountState(**base)  # type: ignore[arg-type]


@pytest.fixture
def brokers() -> dict[str, _Broker]:
    return {}


@pytest.fixture(autouse=True)
def _brokers(app, brokers):
    services = app.state.services
    services.live = LiveService(services.context, brokers=lambda p: brokers.get(p.id))


def _portfolio(settings, person: dict, name: str = "Live") -> str:
    with SqliteState(settings.state.path) as state:
        scope = Scope(user_id=person["id"], role=person["role"])
        return PortfolioRepository(state).create(scope, name=name, kind="broker").id


def _margin_on(settings, *, margin_accounts=True, margin_call=True) -> None:
    from stonks.production.rules._account_settings import AccountRulesSettings
    from stonks.production.rules.margin_call import MarginCallSettings
    from stonks.production.rules.settings import RuleSettings

    rules = RuleSettings(
        account_rules=AccountRulesSettings(enabled=True, margin_accounts=margin_accounts),
        margin_call=MarginCallSettings(enabled=margin_call),
    )
    settings.production.risk = settings.production.risk.model_copy(update={"rules": rules})


def _put(client, pid, body, headers):
    return client.put(f"/api/portfolios/{pid}/live/account-profile", json=body, headers=headers)


def test_margin_is_off_by_default(app, client, settings, people, brokers):
    alice = people["alice"]["headers"]
    pid = _portfolio(settings, people["alice"])
    brokers[pid] = _Broker(account())
    allow_step_up(app)
    got = _put(client, pid, MARGIN, alice)
    assert got.status_code == 409, got.text
    assert "margin accounts are off" in got.json()["detail"].lower()


def test_margin_needs_the_margin_call_rule(app, client, settings, people, brokers):
    pid = _portfolio(settings, people["alice"])
    brokers[pid] = _Broker(account())
    _margin_on(settings, margin_call=False)
    allow_step_up(app)
    got = _put(client, pid, MARGIN, people["alice"]["headers"])
    assert got.status_code == 409 and "margin_call" in got.json()["detail"]


def test_margin_needs_a_fresh_second_factor(client, settings, people, brokers):
    pid = _portfolio(settings, people["alice"])
    brokers[pid] = _Broker(account())
    _margin_on(settings)
    got = _put(client, pid, MARGIN, people["alice"]["headers"])
    assert got.status_code == 403 and got.json()["code"] == "step_up_required"


def test_margin_needs_the_risks_acknowledged(app, client, settings, people, brokers):
    pid = _portfolio(settings, people["alice"])
    brokers[pid] = _Broker(account())
    _margin_on(settings)
    allow_step_up(app)
    got = _put(client, pid, {**MARGIN, "acknowledge_margin_risks": False},
               people["alice"]["headers"])  # fmt: skip
    assert got.status_code == 422 and "risks" in got.json()["detail"]


@pytest.mark.parametrize(
    ("broker", "said"),
    [
        (_Broker(account(reported="cash")), "reports cash"),
        (_Broker(account(reported=None)), "does not report"),
        (_Broker(None, fail=True), "could not read"),
        (_Broker(account(account_type="cash")), "set up as a cash account"),
    ],
)
def test_margin_needs_the_broker_to_report_a_margin_account(
    app, client, settings, people, brokers, broker, said
):
    pid = _portfolio(settings, people["alice"])
    brokers[pid] = broker
    _margin_on(settings)
    allow_step_up(app)
    got = _put(client, pid, MARGIN, people["alice"]["headers"])
    assert got.status_code == 409, got.text
    assert said in got.json()["detail"]
    assert broker.closed == 1


def test_a_margin_profile_is_saved_and_audited(app, client, settings, people, brokers):
    pid = _portfolio(settings, people["alice"])
    brokers[pid] = _Broker(account())
    _margin_on(settings)
    allow_step_up(app)
    got = _put(client, pid, MARGIN, people["alice"]["headers"])
    assert got.status_code == 200, got.text
    assert got.json()["account_type"] == "margin" and got.json()["allow_short"] is True
    assert "acknowledge_margin_risks" not in got.json()
    with SqliteState(settings.state.path) as state:
        rows = state.sql(
            "SELECT details_json FROM audit_log WHERE action = 'live.account_profile_set'"
        )
    assert '"broker_reported_type": "margin"' in rows[-1]["details_json"]
    # a later change of another field keeps margin without asking again
    again = _put(client, pid, {**MARGIN, "acknowledge_margin_risks": False,
                               "client_class": "professional"}, people["alice"]["headers"])  # fmt: skip
    assert again.status_code == 200, again.text


def test_a_cash_profile_needs_no_broker(app, client, settings, people):
    pid = _portfolio(settings, people["alice"])
    allow_step_up(app)
    got = _put(client, pid, {"jurisdiction": "us"}, people["alice"]["headers"])
    assert got.status_code == 200


def test_the_margin_view_shows_buying_power_and_margin_use(app, client, settings, people, brokers):
    pid = _portfolio(settings, people["alice"])
    broker = brokers[pid] = _Broker(account())
    _margin_on(settings)
    allow_step_up(app)
    assert _put(client, pid, MARGIN, people["alice"]["headers"]).status_code == 200
    got = client.get(f"/api/portfolios/{pid}/live/margin", headers=people["alice"]["headers"])
    assert got.status_code == 200, got.text
    view = got.json()
    assert view["profile_type"] == "margin" and view["margin_accounts_on"] is True
    a = view["account"]
    assert (a["equity"], a["buying_power"], a["available_funds"]) == (20_000.0, 20_000.0, 5_000.0)
    assert a["margin_use"] == pytest.approx(0.7)
    assert a["cushion"] == pytest.approx(0.3)
    assert a["level"] == "ok" and a["reported_type"] == "margin"
    # 90% of 20,000 minus the 12,000 of initial margin in use
    assert a["margin_room"] == pytest.approx(6_000.0)
    assert view["buffer"] == pytest.approx(0.1)
    assert (view["warn_cushion"], view["reduce_cushion"]) == (0.15, 0.10)
    pdt = view["pdt"]
    assert pdt["applies"] is True and pdt["day_trades_remaining"] == 2
    assert pdt["equity_threshold"] == 25_000.0
    assert view["read_error"] is None
    assert broker.closed >= 2


def test_the_margin_view_of_a_cash_account(app, client, settings, people, brokers):
    pid = _portfolio(settings, people["alice"])
    brokers[pid] = _Broker(account(reported="cash", account_type="cash", excess=None,
                                   maintenance_margin=0.0, initial_margin=0.0))  # fmt: skip
    got = client.get(f"/api/portfolios/{pid}/live/margin", headers=people["alice"]["headers"])
    view = got.json()
    assert view["profile_type"] is None and view["margin_accounts_on"] is False
    assert view["account"]["level"] is None and view["account"]["margin_use"] == 0.0
    assert view["pdt"]["applies"] is False


def test_the_margin_view_reports_an_unreadable_broker(client, settings, people, brokers):
    pid = _portfolio(settings, people["alice"])
    brokers[pid] = _Broker(None, fail=True)
    view = client.get(f"/api/portfolios/{pid}/live/margin",
                      headers=people["alice"]["headers"]).json()  # fmt: skip
    assert view["account"] is None and "gateway down" in view["read_error"]


def test_the_margin_view_of_another_person_reads_as_missing(client, settings, people):
    pid = _portfolio(settings, people["alice"])
    for who in ("bob", "ada"):
        got = client.get(f"/api/portfolios/{pid}/live/margin", headers=people[who]["headers"])
        assert got.status_code == 404

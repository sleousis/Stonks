"""The ``live_margin`` scheduler action and :func:`run_margin_monitor`
(roadmap 19.13): the margin accounts listed on a gateway, read during the
session through ``FakeIbGateway``. No network."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from stonks.accounts.rules import AccountProfile
from stonks.accounts.rules.profiles import set_profile
from stonks.config import Settings
from stonks.core.clock import FakeClock
from stonks.execution.brokers.ibkr.settings import IbkrBrokerConfig
from stonks.production.live.margin import latest_check, run_margin_monitor
from stonks.production.rules.margin_call import MarginCallSettings
from stonks.scheduling.api_backend import API_ACTIONS
from stonks.scheduling.config import default_jobs
from stonks.scheduling.in_process import IN_PROCESS_ACTIONS
from stonks.scheduling.jobs import JobSpec, RunContext
from stonks.scheduling.local import LOCAL_ACTIONS
from stonks.scheduling.triggers import Fire, IntervalTrigger
from stonks.store.state import SqliteState
from tests.fakes.ib_gateway import T0, FakeIbGateway

GATEWAYS = {
    "paper": {
        "host": "h",
        "port": 4004,
        "mode": "paper",
        "portfolios": ["pf_default"],
        "account_type": "margin",
    }
}


def _settings(tmp_path, gateways=None) -> Settings:
    s = Settings(
        state={"path": tmp_path / "state.sqlite"},
        notify={"backends": []},
        brokers={"ibkr": {"gateways": gateways or {}}},
    )
    with SqliteState(s.state.path) as state:
        state.migrate()
    return s


def _ctx(settings) -> RunContext:
    return RunContext(
        spec=JobSpec("live_margin", "live_margin", IntervalTrigger(timedelta(minutes=30))),
        fire=Fire(T0, date(2026, 9, 28), "k"),
        run_id="srun_t",
        now=T0,
        settings=settings,
        notifier=None,  # type: ignore[arg-type]
    )


def test_the_job_is_a_default_job_every_half_hour():
    [job] = [j for j in default_jobs() if j.name == "live_margin"]
    assert job.action == "live_margin" and job.catch_up == "none"
    assert job.trigger.every_minutes == 30  # type: ignore[union-attr]


@pytest.mark.parametrize("registry", [LOCAL_ACTIONS, API_ACTIONS, IN_PROCESS_ACTIONS])
def test_the_job_skips_without_gateways(tmp_path, registry):
    out = registry.get("live_margin")(_ctx(_settings(tmp_path)))
    assert out.status == "skipped" and out.detail["reason"] == "no_gateways"


def test_the_job_skips_without_a_margin_profile(tmp_path):
    out = LOCAL_ACTIONS.get("live_margin")(_ctx(_settings(tmp_path, GATEWAYS)))
    assert out.status == "skipped" and out.detail["reason"] == "no_margin_accounts"


def _margin_profile(state) -> None:
    set_profile(
        state,
        AccountProfile("pf_default", "us", account_type="margin", allow_short=True),
        actor="user:u",
    )


def test_the_monitor_reads_each_margin_account_on_the_reconcile_client(state):
    _margin_profile(state)
    seen: dict[int, FakeIbGateway] = {}

    def factory(endpoint):
        gw = FakeIbGateway()
        gw.margin_account(equity=100_000, excess_liquidity=8_000, maintenance=92_000)
        seen[endpoint.client_id] = gw
        return gw

    sent = []
    [result] = run_margin_monitor(
        state,
        IbkrBrokerConfig(gateways=GATEWAYS),
        MarginCallSettings(),
        clock=FakeClock(T0),
        client_factory=factory,
        publish=sent.append,
    )
    assert list(seen) == [14]
    assert result.portfolio_id == "pf_default" and result.check is not None
    assert result.check.level == "reduce"
    check = latest_check(state, "pf_default")
    assert check is not None and check.source == "monitor"
    assert len(sent) == 1 and sent[0].urgency == "high"


def test_an_unreadable_account_is_reported_not_raised(state):
    _margin_profile(state)

    def factory(endpoint):
        gw = FakeIbGateway()
        gw.connect_failures = 99
        return gw

    config = IbkrBrokerConfig(gateways=GATEWAYS, reconnect_deadline_seconds=0.01)
    [result] = run_margin_monitor(
        state, config, MarginCallSettings(), clock=FakeClock(T0), client_factory=factory,
        publish=lambda e: None,
    )  # fmt: skip
    assert result.check is None and result.error
    assert latest_check(state, "pf_default") is None


def test_the_job_fails_when_an_account_needs_reducing(tmp_path, monkeypatch):
    settings = _settings(tmp_path, GATEWAYS)
    with SqliteState(settings.state.path) as state:
        _margin_profile(state)

    def factory(endpoint):
        gw = FakeIbGateway()
        gw.margin_account(equity=100_000, excess_liquidity=-500, maintenance=100_500)
        return gw

    import stonks.execution.brokers.ibkr.factory as ibkr_factory

    monkeypatch.setattr(ibkr_factory, "default_client_factory", factory)
    out = LOCAL_ACTIONS.get("live_margin")(_ctx(settings))
    assert out.status == "failed" and out.alerted
    assert out.detail["levels"] == {"pf_default": "call"}

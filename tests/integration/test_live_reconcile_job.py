"""The ``live_reconcile`` scheduler action and :func:`run_gateway_checks`
(roadmap 19.5): every portfolio listed on a gateway, through
``FakeIbGateway``. No network."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from stonks.config import Settings
from stonks.core.clock import FakeClock
from stonks.execution.brokers.ibkr.settings import IbkrBrokerConfig
from stonks.production.live.checks import list_reports, run_gateway_checks
from stonks.scheduling.api_backend import API_ACTIONS
from stonks.scheduling.in_process import IN_PROCESS_ACTIONS
from stonks.scheduling.jobs import JobSpec, RunContext
from stonks.scheduling.local import LOCAL_ACTIONS
from stonks.scheduling.triggers import Fire, SessionTrigger
from stonks.store.state import SqliteState
from tests.fakes.ib_gateway import AAPL, T0, FakeIbGateway


def _settings(tmp_path, gateways=None) -> Settings:
    s = Settings(
        state={"path": tmp_path / "state.sqlite"},
        notify={"backends": []},
        brokers={"ibkr": {"gateways": gateways or {}}},
    )
    with SqliteState(s.state.path) as state:
        state.migrate()
    return s


def _ctx(settings, kind="sod") -> RunContext:
    return RunContext(
        spec=JobSpec(
            f"live_{kind}_check",
            "live_reconcile",
            SessionTrigger("XNYS", "open", timedelta(minutes=-60)),
            params={"kind": kind},
        ),
        fire=Fire(T0, date(2026, 9, 28), "k"),
        run_id="srun_t",
        now=T0,
        settings=settings,
        notifier=None,  # type: ignore[arg-type]
    )


@pytest.mark.parametrize("registry", [LOCAL_ACTIONS, API_ACTIONS, IN_PROCESS_ACTIONS])
def test_the_job_skips_without_live_portfolios(tmp_path, registry):
    out = registry.get("live_reconcile")(_ctx(_settings(tmp_path)))
    assert out.status == "skipped" and out.detail["reason"] == "no_gateways"


def test_a_gateway_without_portfolios_is_skipped_too(tmp_path):
    gw = {"paper": {"host": "h", "port": 4004, "mode": "paper"}}
    out = LOCAL_ACTIONS.get("live_reconcile")(_ctx(_settings(tmp_path, gw)))
    assert out.status == "skipped"


def test_an_unreachable_gateway_fails_the_job_with_an_outage_report(tmp_path):
    import socket

    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()  # nothing listens there
    gw = {
        "paper": {"host": "127.0.0.1", "port": port, "mode": "paper", "portfolios": ["pf_default"]}
    }
    settings = _settings(tmp_path, gw)
    settings.brokers.ibkr.connect_timeout_seconds = 0.5
    settings.brokers.ibkr.reconnect_deadline_seconds = 0.1
    out = LOCAL_ACTIONS.get("live_reconcile")(_ctx(settings))
    assert out.status == "failed" and out.alerted
    assert out.detail["statuses"] == {"pf_default": "outage"}
    with SqliteState(settings.state.path) as state:
        [report] = list_reports(state)
    assert report.kind == "sod" and report.status == "outage"


def test_run_gateway_checks_uses_the_reconcile_client_id(state):
    gateways: dict[str, FakeIbGateway] = {}

    def factory(endpoint):
        gw = FakeIbGateway()
        gw.set_values(NetLiquidation="1000", TotalCashValue="1000")
        gw.set_position(AAPL, 3)  # the owner's own shares
        gateways[endpoint.client_id] = gw
        return gw

    config = IbkrBrokerConfig(
        gateways={
            "paper": {"host": "h", "port": 4004, "mode": "paper", "portfolios": ["pf_default"]}
        }
    )
    [result] = run_gateway_checks(
        state,
        config,
        "eod",
        clock=FakeClock(T0),
        client_factory=factory,
        publish=lambda e: None,
    )
    assert list(gateways) == [14]
    assert result.status == "clean"
    assert result.report.external["positions"] == {"AAPL.US": 3.0}

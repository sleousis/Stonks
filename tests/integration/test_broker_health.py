"""Broker gateway health (roadmap 19.4): status per gateway, one
high-urgency alert a day, auto paused after a long outage or a real fault,
a recovery note, the health checks and the Sunday re-login reminder."""

from __future__ import annotations

import socket
from datetime import UTC, datetime

import pytest

from stonks.execution.brokers.ibkr.settings import IbkrBrokerConfig, IbkrHealthSettings
from stonks.production.broker_health import (
    GatewayTarget,
    ProbeResult,
    SocketProbe,
    check_gateways,
    gateway_health_checks,
    gateway_statuses,
    gateway_targets,
    send_reauth_reminder,
)

MON = datetime(2026, 9, 28, 13, 0, tzinfo=UTC)
TUE = datetime(2026, 9, 29, 13, 0, tzinfo=UTC)
TARGET = GatewayTarget(
    name="paper", host="ib-gateway-paper", port=4004, mode="paper", portfolios=("pf_default",)
)
SETTINGS = IbkrHealthSettings(alert_after_failures=2, pause_after_sessions=2)


class FakeProbe:
    def __init__(self, *results: ProbeResult) -> None:
        self.results = list(results)

    def probe(self, target):
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


UP = ProbeResult(connected=True, detail="ok", latency_ms=3.0)
DOWN = ProbeResult(connected=False, detail="ConnectionRefusedError")


def _auto_subscription(state) -> str:
    state.execute(
        "INSERT INTO strategies (id, class_path, params_json, status, created_at, updated_at)"
        " VALUES ('s1', 'x.Y', '{}', 'active', 'x', 'x')"
    )
    state.execute(
        "INSERT INTO subscriptions (id, user_id, strategy_id, portfolio_id, mode, weight,"
        " created_at, updated_at) SELECT 'sub_1', owner_id, 's1', id, 'auto', 1.0, 'x', 'x'"
        " FROM portfolios WHERE id = 'pf_default'"
    )
    return "sub_1"


def test_a_healthy_gateway_is_stored(state):
    sent: list = []
    (check,) = check_gateways(
        state, [TARGET], FakeProbe(UP), SETTINGS, now=MON, publish=sent.append
    )
    assert check.status.connected and check.status.last_ok_at == check.status.last_check_at
    assert not sent
    (hc,) = gateway_health_checks(state)
    assert hc.name == "broker:paper" and hc.ok


def test_a_down_gateway_fails_health_but_never_opens_the_operational_halt(state, lake):
    from stonks.config import HealthConfig
    from stonks.production.halts import run_health

    check_gateways(state, [TARGET], FakeProbe(DOWN), SETTINGS, now=MON, publish=lambda e: 0)
    report = run_health(state, lake, [], HealthConfig())
    broker = [c for c in report.checks if c.name == "broker:paper"]
    assert broker and not broker[0].ok
    kinds = [r["kind"] for r in state.sql("SELECT kind FROM risk_halts")]
    assert "operational" not in kinds


def test_repeated_failures_alert_once_a_day_then_recover(state):
    sent: list = []
    probe = FakeProbe(DOWN, DOWN, DOWN, UP)
    first = check_gateways(state, [TARGET], probe, SETTINGS, now=MON, publish=sent.append)[0]
    assert not first.alerted and not sent
    second = check_gateways(state, [TARGET], probe, SETTINGS, now=MON, publish=sent.append)[0]
    assert second.alerted
    assert {e.urgency for e in sent} == {"high"}
    assert len(sent) == 2  # the owner and the admins
    third = check_gateways(state, [TARGET], probe, SETTINGS, now=MON, publish=sent.append)[0]
    assert not third.alerted and len(sent) == 2
    (hc,) = gateway_health_checks(state)
    assert not hc.ok and "3 failed checks" in hc.detail
    back = check_gateways(state, [TARGET], probe, SETTINGS, now=MON, publish=sent.append)[0]
    assert back.recovered and back.status.alerted_at is None
    assert "back" in sent[-1].title


def test_an_outage_over_two_sessions_pauses_auto(state, monkeypatch):
    monkeypatch.setattr("stonks.production.auto_pause._notify_owner", lambda *a: None)
    sub = _auto_subscription(state)
    probe = FakeProbe(DOWN, DOWN)
    monday = check_gateways(state, [TARGET], probe, SETTINGS, now=MON, publish=lambda e: None)[0]
    assert monday.paused == ()
    tuesday = check_gateways(state, [TARGET], probe, SETTINGS, now=TUE, publish=lambda e: None)[0]
    assert tuesday.paused == (sub,)
    reason = state.sql("SELECT paused_reason FROM subscriptions WHERE id = ?", [sub])[0][0]
    assert reason.startswith("broker_error: gateway paper")


def test_a_real_fault_pauses_at_once(state, monkeypatch):
    monkeypatch.setattr("stonks.production.auto_pause._notify_owner", lambda *a: None)
    sub = _auto_subscription(state)
    fault = ProbeResult(connected=False, detail="account U1 is not DU", fault="wrong_account")
    (check,) = check_gateways(
        state, [TARGET], FakeProbe(fault), SETTINGS, now=MON, publish=lambda e: None
    )
    assert check.paused == (sub,)
    assert gateway_statuses(state)[0].fault == "wrong_account"


def test_a_probe_that_raises_counts_as_down(state):
    (check,) = check_gateways(
        state, [TARGET], FakeProbe(RuntimeError("boom")), SETTINGS, now=MON, publish=lambda e: 0
    )
    assert not check.status.connected and "boom" in (check.status.detail or "")


def test_socket_probe_against_a_loopback_listener():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    try:
        up = SocketProbe(1.0).probe(GatewayTarget("x", "127.0.0.1", port, "paper"))
        assert up.connected and up.latency_ms is not None
    finally:
        server.close()
    down = SocketProbe(0.5).probe(GatewayTarget("x", "127.0.0.1", port, "paper"))
    assert not down.connected


def test_the_reminder_goes_once_per_audience_and_week(state):
    sent: list = []
    targets = [TARGET, GatewayTarget("live", "ib-gateway-live", 4003, "live")]
    assert send_reauth_reminder(state, targets, now=MON, publish=sent.append) == 2
    keys = {e.dedupe_key for e in sent}
    assert keys == {"ibkr_reauth:2026-W40:pf:pf_default", "ibkr_reauth:2026-W40:admins"}
    assert all(e.urgency == "normal" for e in sent)


def test_targets_from_config_and_secrets_refused():
    config = IbkrBrokerConfig.model_validate(
        {
            "gateways": {
                "paper": {"host": "h", "port": 4004, "mode": "paper", "portfolios": ["pf_a"]},
                "live": {"host": "h2", "port": 4003, "mode": "live", "account_id": "U123"},
            }
        }
    )
    names = [t.name for t in gateway_targets(config)]
    assert names == ["live", "paper"]
    with pytest.raises(ValueError, match="secret"):
        IbkrBrokerConfig.model_validate({"password": "hunter2"})
    with pytest.raises(ValueError, match="DU"):
        IbkrBrokerConfig.model_validate(
            {"gateways": {"p": {"host": "h", "port": 1, "mode": "paper", "account_id": "U1"}}}
        )
    with pytest.raises(ValueError, match="listed on gateways"):
        IbkrBrokerConfig.model_validate(
            {
                "gateways": {
                    "a": {"host": "h", "port": 1, "mode": "paper", "portfolios": ["pf"]},
                    "b": {"host": "h", "port": 2, "mode": "live", "portfolios": ["pf"]},
                }
            }
        )

"""Broker gateway health, outage handling and the weekly re-login reminder
(roadmap 19.4, ``docs/design/live-trading.md`` section 3).

The ``broker_health`` job runs every few minutes. For each gateway in
``[brokers.ibkr.gateways]`` it asks a :class:`BrokerProbe` whether the
gateway answers and stores the result in ``broker_gateway_status``:

- a gateway that fails ``alert_after_failures`` checks in a row sends one
  high-urgency push a day to the owners of its portfolios and the admins,
  with the runbook link;
- a gateway down for ``pause_after_sessions`` trading sessions, or with a
  real fault (login refused, wrong account, competing session), pauses the
  auto subscriptions of its portfolios through the existing ``pause_auto``
  (audit row, owner notified, resume needs a fresh second factor);
- a short outage never sends yesterday's decisions late: the book skips,
  and the next day decides again;
- a gateway that answers again sends a recovery note and clears its alert.

The global operational halt never trips on a broker outage: a broker
belongs to portfolios, so only their books skip or pause. The health
checks are named ``broker:<gateway>``, outside the operational checks.

:class:`SocketProbe` only checks that the gateway's API port accepts a
connection. The IBKR adapter (roadmap 19.2) adds a probe that logs in,
checks the managed account and reads the server time.

:func:`send_reauth_reminder` is the Sunday push: approve the IBKR login on
your phone tonight (the weekly session reset needs a second factor).
"""

from __future__ import annotations

import socket
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Protocol

from stonks.execution.brokers.ibkr.settings import (
    GatewayMode,
    IbkrBrokerConfig,
    IbkrHealthSettings,
)
from stonks.logging import get_logger
from stonks.production.auto_pause import broker_error_reason, pause_auto
from stonks.production.health import HealthCheck
from stonks.store.state import SqliteState

_log = get_logger("stonks.production.broker_health")

TABLE = "broker_gateway_status"
RUNBOOK_LINK = "/health"
#: Name prefix of the broker health checks (outside the operational checks).
CHECK_PREFIX = "broker"

Publish = Callable[[Any], Any]


@dataclass(frozen=True)
class GatewayTarget:
    name: str
    host: str
    port: int
    mode: GatewayMode
    portfolios: tuple[str, ...] = ()
    account_id: str | None = None


@dataclass(frozen=True)
class ProbeResult:
    connected: bool
    detail: str
    latency_ms: float | None = None
    #: A real fault that pauses auto at once (``login_refused``,
    #: ``wrong_account``, ``competing_session``), else ``None``.
    fault: str | None = None


class BrokerProbe(Protocol):
    def probe(self, target: GatewayTarget) -> ProbeResult: ...


class SocketProbe:
    """Is the gateway's API port accepting connections?"""

    def __init__(self, timeout_seconds: float = 2.0) -> None:
        self.timeout = timeout_seconds

    def probe(self, target: GatewayTarget) -> ProbeResult:
        start = time.perf_counter()
        try:
            with socket.create_connection((target.host, target.port), timeout=self.timeout):
                pass
        except OSError as exc:
            return ProbeResult(connected=False, detail=f"{type(exc).__name__}: {exc}")
        latency = (time.perf_counter() - start) * 1000.0
        return ProbeResult(connected=True, detail="port open", latency_ms=latency)


@dataclass(frozen=True)
class GatewayStatus:
    gateway: str
    mode: GatewayMode
    connected: bool
    last_check_at: str
    last_ok_at: str | None
    down_since: str | None
    consecutive_failures: int
    fault: str | None
    detail: str | None
    latency_ms: float | None
    alerted_at: str | None
    paused_at: str | None


@dataclass(frozen=True)
class GatewayCheck:
    """What one check found and did."""

    status: GatewayStatus
    alerted: bool = False
    recovered: bool = False
    paused: tuple[str, ...] = ()


def gateway_targets(config: IbkrBrokerConfig) -> list[GatewayTarget]:
    return [
        GatewayTarget(
            name=name,
            host=gw.host,
            port=gw.port,
            mode=gw.mode,
            portfolios=tuple(gw.portfolios),
            account_id=gw.account_id,
        )
        for name, gw in sorted(config.gateways.items())
    ]


def status_enabled(state: SqliteState) -> bool:
    rows = state.sql("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", [TABLE])
    return bool(rows)


def _iso(when: datetime) -> str:
    return when.astimezone(UTC).isoformat(timespec="seconds")


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _row(r: Any) -> GatewayStatus:
    return GatewayStatus(
        gateway=r["gateway"],
        mode=r["mode"],
        connected=bool(r["connected"]),
        last_check_at=r["last_check_at"],
        last_ok_at=r["last_ok_at"],
        down_since=r["down_since"],
        consecutive_failures=int(r["consecutive_failures"]),
        fault=r["fault"],
        detail=r["detail"],
        latency_ms=r["latency_ms"],
        alerted_at=r["alerted_at"],
        paused_at=r["paused_at"],
    )


def gateway_statuses(state: SqliteState) -> list[GatewayStatus]:
    if not status_enabled(state):
        return []
    return [_row(r) for r in state.sql(f"SELECT * FROM {TABLE} ORDER BY gateway")]


def get_status(state: SqliteState, gateway: str) -> GatewayStatus | None:
    rows = state.sql(f"SELECT * FROM {TABLE} WHERE gateway = ?", [gateway])
    return _row(rows[0]) if rows else None


def sessions_down(since: date, until: date, calendar: str) -> int:
    """Trading sessions from ``since`` to ``until`` (both included)."""
    from stonks.scheduling.calendar import get_calendar

    if until < since:
        return 0
    return len(get_calendar(calendar).sessions(since, until))


def check_gateways(
    state: SqliteState,
    targets: Sequence[GatewayTarget],
    probe: BrokerProbe,
    settings: IbkrHealthSettings,
    *,
    now: datetime | None = None,
    publish: Publish | None = None,
) -> list[GatewayCheck]:
    """Probe every gateway, store its status, and alert or pause as the
    module doc says. A probe that raises counts as down."""
    now = now or datetime.now(UTC)
    return [_check_one(state, t, probe, settings, now, publish) for t in targets]


def _check_one(
    state: SqliteState,
    target: GatewayTarget,
    probe: BrokerProbe,
    settings: IbkrHealthSettings,
    now: datetime,
    publish: Publish | None,
) -> GatewayCheck:
    try:
        result = probe.probe(target)
    except Exception as exc:
        result = ProbeResult(connected=False, detail=f"probe failed: {type(exc).__name__}: {exc}")
    old = get_status(state, target.name)
    stamp = _iso(now)
    if result.connected:
        status = GatewayStatus(
            gateway=target.name,
            mode=target.mode,
            connected=True,
            last_check_at=stamp,
            last_ok_at=stamp,
            down_since=None,
            consecutive_failures=0,
            fault=None,
            detail=result.detail,
            latency_ms=result.latency_ms,
            alerted_at=None,
            paused_at=None,
        )
        _save(state, status)
        recovered = old is not None and old.alerted_at is not None
        if recovered:
            _notify(state, target, publish, recovered=True, detail=result.detail, now=now)
        return GatewayCheck(status=status, recovered=recovered)

    failures = (old.consecutive_failures if old else 0) + 1
    down_since = (old.down_since if old and old.down_since else None) or stamp
    alerted_at = old.alerted_at if old else None
    paused_at = old.paused_at if old else None
    status = GatewayStatus(
        gateway=target.name,
        mode=target.mode,
        connected=False,
        last_check_at=stamp,
        last_ok_at=old.last_ok_at if old else None,
        down_since=down_since,
        consecutive_failures=failures,
        fault=result.fault,
        detail=result.detail,
        latency_ms=None,
        alerted_at=alerted_at,
        paused_at=paused_at,
    )
    alerted = False
    alerted_day = _parse(alerted_at)
    if failures >= settings.alert_after_failures and (
        alerted_day is None or alerted_day.date() < now.date()
    ):
        _notify(state, target, publish, recovered=False, detail=result.detail, now=now)
        status = _replace(status, alerted_at=stamp)
        alerted = True
    paused: tuple[str, ...] = ()
    since = _parse(down_since) or now
    long_outage = (
        sessions_down(since.date(), now.date(), settings.calendar) >= settings.pause_after_sessions
    )
    if (result.fault is not None or long_outage) and target.portfolios:
        why = result.fault or f"down since {down_since}"
        paused = _pause(state, target, f"gateway {target.name} {why}: {result.detail}", now)
        if paused or status.paused_at is None:
            status = _replace(status, paused_at=stamp)
    _save(state, status)
    _log.warning(
        "broker_health.gateway_down",
        gateway=target.name,
        failures=failures,
        fault=result.fault,
        alerted=alerted,
        paused=list(paused),
    )
    return GatewayCheck(status=status, alerted=alerted, paused=paused)


def _replace(status: GatewayStatus, **changes: Any) -> GatewayStatus:
    from dataclasses import replace

    return replace(status, **changes)


def _save(state: SqliteState, s: GatewayStatus) -> None:
    state.execute(
        f"INSERT INTO {TABLE} (gateway, mode, connected, last_check_at, last_ok_at, down_since,"
        " consecutive_failures, fault, detail, latency_ms, alerted_at, paused_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT (gateway) DO UPDATE SET mode = excluded.mode,"
        " connected = excluded.connected, last_check_at = excluded.last_check_at,"
        " last_ok_at = excluded.last_ok_at, down_since = excluded.down_since,"
        " consecutive_failures = excluded.consecutive_failures, fault = excluded.fault,"
        " detail = excluded.detail, latency_ms = excluded.latency_ms,"
        " alerted_at = excluded.alerted_at, paused_at = excluded.paused_at",
        [
            s.gateway,
            s.mode,
            int(s.connected),
            s.last_check_at,
            s.last_ok_at,
            s.down_since,
            s.consecutive_failures,
            s.fault,
            (s.detail or "")[:300] or None,
            s.latency_ms,
            s.alerted_at,
            s.paused_at,
        ],
    )


def _pause(state: SqliteState, target: GatewayTarget, why: str, now: datetime) -> tuple[str, ...]:
    if not state.sql("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'subscriptions'"):
        return ()
    paused: list[str] = []
    for pid in target.portfolios:
        ids = [
            r["id"]
            for r in state.sql(
                "SELECT id FROM subscriptions WHERE portfolio_id = ? AND mode = 'auto'"
                " AND paused_reason IS NULL ORDER BY id",
                [pid],
            )
        ]
        paused += pause_auto(
            state, pid, ids, broker_error_reason(why), tick_id=None, as_of=now.date()
        )
    return tuple(paused)


def _notify(
    state: SqliteState,
    target: GatewayTarget,
    publish: Publish | None,
    *,
    recovered: bool,
    detail: str,
    now: datetime,
) -> None:
    """A failed send is logged, never raised: the status is already stored."""
    from stonks.notify.events import Audience, Event

    send = publish or _router(state)
    day = now.date().isoformat()
    audiences = [Audience.owner_of(pid) for pid in target.portfolios] + [Audience.admins()]
    for audience in audiences:
        if recovered:
            event = Event(
                category="system",
                level="info",
                title=f"Broker gateway {target.name} is back",
                body="The IB Gateway answers again. Paused auto subscriptions stay paused "
                "until you resume them.",
                audience=audience,
                dedupe_key=f"gateway_up:{target.name}:{day}:{audience.portfolio_id or 'admins'}",
                deep_link=RUNBOOK_LINK,
                portfolio_id=audience.portfolio_id,
            )
        else:
            event = Event(
                category="risk",
                level="error",
                urgency="high",
                title=f"Broker gateway {target.name} is down",
                body=f"Stonks cannot reach the IB Gateway ({target.mode}): {detail}. "
                "No orders go out while it is down. Check the gateway container and your "
                "phone for a pending IBKR login. See the broker outage runbook.",
                audience=audience,
                dedupe_key=f"gateway_down:{target.name}:{day}:{audience.portfolio_id or 'admins'}",
                deep_link=RUNBOOK_LINK,
                portfolio_id=audience.portfolio_id,
            )
        try:
            send(event)
        except Exception as exc:
            _log.error("broker_health.notify_failed", gateway=target.name, error=str(exc))


def _router(state: SqliteState) -> Publish:
    from stonks.notify.router import configured_router

    return configured_router(state).publish


def gateway_health_checks(state: SqliteState) -> list[HealthCheck]:
    """One ``broker:<gateway>`` check per gateway the job has seen."""
    checks: list[HealthCheck] = []
    for s in gateway_statuses(state):
        if s.connected:
            detail = f"{s.mode} gateway answers (checked {s.last_check_at})"
        else:
            detail = (
                f"{s.mode} gateway down since {s.down_since}, "
                f"{s.consecutive_failures} failed checks: {s.detail or 'no detail'}"
            )
        checks.append(
            HealthCheck(name=f"{CHECK_PREFIX}:{s.gateway}", ok=s.connected, detail=detail)
        )
    return checks


def send_reauth_reminder(
    state: SqliteState,
    targets: Sequence[GatewayTarget],
    *,
    now: datetime | None = None,
    publish: Publish | None = None,
) -> int:
    """The Sunday push to the owners of every gateway's portfolios (and the
    admins when a gateway has none): approve the IBKR login on your phone
    tonight. One per audience per ISO week. Returns the events sent."""
    from stonks.notify.events import Audience, Event

    now = now or datetime.now(UTC)
    year, week, _ = now.isocalendar()
    send = publish or _router(state)
    audiences: dict[str, Audience] = {}
    for target in targets:
        for pid in target.portfolios:
            audiences.setdefault(f"pf:{pid}", Audience.owner_of(pid))
        if not target.portfolios:
            audiences.setdefault("admins", Audience.admins())
    sent = 0
    for key, audience in audiences.items():
        event = Event(
            category="system",
            level="info",
            urgency="normal",
            title="Approve the IBKR login tonight",
            body="Interactive Brokers resets the gateway session this weekend. Approve the "
            "login request in IBKR Mobile when it arrives, or the gateway stays logged out.",
            audience=audience,
            dedupe_key=f"ibkr_reauth:{year}-W{week:02d}:{key}",
            deep_link=RUNBOOK_LINK,
            portfolio_id=audience.portfolio_id,
        )
        try:
            send(event)
            sent += 1
        except Exception as exc:
            _log.error("broker_health.reminder_failed", audience=key, error=str(exc))
    return sent

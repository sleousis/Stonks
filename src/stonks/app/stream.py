"""The live engine status for the console and the MCP server (roadmap 21.3.4).

The engine runs in its own process and writes its ``engine_status`` row
(:mod:`stonks.engine.status`). This service reads that row and adds what a
person needs to judge it: is the engine live, is its market open, is the
dead-man about to fire, how is the stream, how fast does it decide.

``intraday_pnl`` says whether intraday P&L rows are kept (roadmap 21.3.3,
``[production.intraday_pnl]``). The rows themselves are per portfolio and
come from ``GET /api/risk/intraday``, scoped to the caller's portfolios.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import BaseModel

from stonks.app.context import AppContext
from stonks.auth.policy import Permission, require
from stonks.auth.principal import Principal
from stonks.engine.deadman import CalendarFor, silence_of
from stonks.engine.monitor import LatencyHistogram
from stonks.engine.status import EngineStatus, EngineStatusStore
from stonks.notify.store import redact_text
from stonks.scheduling.calendar import get_calendar

DeadmanState = Literal["ok", "silent", "closed", "stopped"]

#: Stream error text can carry a vendor URL. Keys named here are scrubbed too.
_SECRET_ENV = ("EODHD_API_KEY",)


class LatencyView(BaseModel):
    count: int
    #: Upper estimates from the histogram buckets. ``null`` with no reading.
    p50_seconds: float | None
    p95_seconds: float | None
    mean_seconds: float | None
    max_seconds: float | None


class StreamHealthView(BaseModel):
    source: str
    #: idle, connecting, streaming, backoff, stopped or failed.
    state: str
    connected: bool
    connected_at: datetime | None
    last_event_at: datetime | None
    last_event_age_seconds: float | None
    connects: int
    disconnects: int
    bars_written: int
    late_ticks: int
    write_errors: int
    gaps: int
    backfills_ok: int
    backfills_failed: int
    last_error: str | None


class EngineView(BaseModel):
    engine_id: str
    calendar: str
    state: str
    #: Running and reporting within ``stale_after_seconds``.
    live: bool
    started_at: datetime
    updated_at: datetime
    stopped_at: datetime | None
    last_dispatch_at: datetime | None
    last_dispatch_age_seconds: float | None
    market_open: bool
    #: ``silent``: no bar close for ``deadman_minutes`` in market hours.
    deadman: DeadmanState
    silent_seconds: float | None
    bar_closes: int
    bars: int
    late_bars: int
    pending_closes: int
    handler_errors: dict[str, int]
    stream: StreamHealthView | None
    dispatch_lag: LatencyView
    event_to_order: LatencyView


class IntradayPnlView(BaseModel):
    #: ``[production.intraday_pnl] enabled`` and the snapshot table exists.
    available: bool
    #: Minutes between stored rows per book.
    snapshot_minutes: int
    note: str


class StreamStatusView(BaseModel):
    as_of: datetime
    streaming_enabled: bool
    source: str
    deadman_minutes: int
    stale_after_seconds: float
    engines: list[EngineView]
    intraday_pnl: IntradayPnlView


INTRADAY_PNL_ON = (
    "Intraday P&L per portfolio from the engine's live marks, stored every {minutes} minutes. "
    "Daily P&L stays on the portfolio pages."
)
INTRADAY_PNL_OFF = (
    "Intraday P&L is not kept on this server, so there is nothing to show here. "
    "Daily P&L stays on the portfolio pages."
)


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 6)


def latency_view(h: LatencyHistogram) -> LatencyView:
    n = h.count
    return LatencyView(
        count=n,
        p50_seconds=_round(h.quantile(0.5)),
        p95_seconds=_round(h.quantile(0.95)),
        mean_seconds=_round(h.total / n) if n else None,
        max_seconds=_round(h.max) if n else None,
    )


def _secrets() -> list[str]:
    return [v for v in (os.environ.get(k) for k in _SECRET_ENV) if v]


def _stream_view(status: EngineStatus, now: datetime, live: bool) -> StreamHealthView | None:
    h = status.stream
    if h is None:
        return None
    age = (now - h.last_event_at).total_seconds() if h.last_event_at else None
    return StreamHealthView(
        source=h.source,
        state=h.state if live else "stopped",
        connected=live and h.state == "streaming",
        connected_at=h.connected_at,
        last_event_at=h.last_event_at,
        last_event_age_seconds=_round(max(age, 0.0)) if age is not None else None,
        connects=h.connects,
        disconnects=h.disconnects,
        bars_written=h.bars_written,
        late_ticks=h.late_ticks,
        write_errors=h.write_errors,
        gaps=sum(h.gaps_total.values()),
        backfills_ok=h.backfills_ok,
        backfills_failed=h.backfills_failed,
        last_error=redact_text(h.last_error, _secrets()) if h.last_error else None,
    )


def _market_open(status: EngineStatus, now: datetime, calendar_for: CalendarFor) -> bool:
    try:
        return calendar_for(status.calendar).is_open_at(now)
    except Exception:  # an unknown calendar reads as closed
        return False


def engine_view(
    status: EngineStatus,
    now: datetime,
    *,
    deadman_minutes: int,
    stale_after: timedelta,
    calendar_for: CalendarFor = get_calendar,
    fallback_calendar: str = "XNYS",
) -> EngineView:
    live = status.is_live(now, stale_after=stale_after)
    market_open = _market_open(status, now, calendar_for)
    silence = silence_of(
        status,
        now,
        minutes=deadman_minutes,
        calendar_for=calendar_for,
        fallback_calendar=fallback_calendar,
    )
    deadman: DeadmanState
    if status.stopped_at is not None:
        deadman = "stopped"
    elif silence is not None:
        deadman = "silent"
    elif not market_open:
        deadman = "closed"
    else:
        deadman = "ok"
    driver = status.driver
    last = status.last_dispatch_at
    return EngineView(
        engine_id=status.engine_id,
        calendar=status.calendar,
        state=status.state if live or status.stopped_at else "no heartbeat",
        live=live,
        started_at=status.started_at,
        updated_at=status.updated_at,
        stopped_at=status.stopped_at,
        last_dispatch_at=last,
        last_dispatch_age_seconds=_round(max((now - last).total_seconds(), 0.0)) if last else None,
        market_open=market_open,
        deadman=deadman,
        silent_seconds=_round(silence.silent_seconds) if silence else None,
        bar_closes=int(driver.get("bar_closes") or 0),
        bars=int(driver.get("bars") or 0),
        late_bars=int(driver.get("late_bars") or 0),
        pending_closes=int(driver.get("pending_closes") or 0),
        handler_errors={
            k: v for k, v in status.handler_errors().items() if not k.startswith("monitor.")
        },
        stream=_stream_view(status, now, live),
        dispatch_lag=latency_view(status.latency("dispatch_lag")),
        event_to_order=latency_view(status.latency("event_to_order")),
    )


class StreamService:
    def __init__(
        self,
        ctx: AppContext,
        *,
        clock: Callable[[], datetime] | None = None,
        calendar_for: CalendarFor = get_calendar,
    ) -> None:
        self._ctx = ctx
        self._clock = clock or (lambda: datetime.now(UTC))
        self._calendar_for = calendar_for

    def status(self, principal: Principal) -> StreamStatusView:
        require(principal, Permission.READ)
        settings = self._ctx.settings.streaming
        monitor = settings.monitor
        stale = timedelta(seconds=monitor.stale_after_seconds)
        now = self._clock()
        engines = [
            engine_view(
                s,
                now,
                deadman_minutes=monitor.deadman_minutes,
                stale_after=stale,
                calendar_for=self._calendar_for,
                fallback_calendar=settings.calendar,
            )
            for s in EngineStatusStore(self._ctx.settings.state.path).read_all()
        ]
        return StreamStatusView(
            as_of=now,
            streaming_enabled=settings.enabled,
            source=settings.source,
            deadman_minutes=monitor.deadman_minutes,
            stale_after_seconds=monitor.stale_after_seconds,
            engines=engines,
            intraday_pnl=self._pnl_view(),
        )

    def _pnl_view(self) -> IntradayPnlView:
        from stonks.production.intraday_pnl import intraday_snapshots_enabled

        cfg = self._ctx.settings.production.intraday_pnl
        with self._ctx.state() as state:
            available = cfg.enabled and intraday_snapshots_enabled(state)
        note = (
            INTRADAY_PNL_ON.format(minutes=cfg.snapshot_minutes) if available else INTRADAY_PNL_OFF
        )
        return IntradayPnlView(
            available=available, snapshot_minutes=cfg.snapshot_minutes, note=note
        )

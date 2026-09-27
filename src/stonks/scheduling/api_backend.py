"""The ``api`` backend: start jobs through the running REST API.

``stonks serve`` holds the DuckDB lake read-write and DuckDB allows one
writing process per file, so a separate scheduler process must not open
it. This backend starts each job on the API's own job routes and waits on
``GET /api/jobs/{id}``, exactly as the MCP server does:

- ``ingest_prices``: ``POST /api/ingest/runs`` (kind ``prices``) for the
  universe over the last ``lookback_days`` up to the fire's date;
- ``ingest_metadata``: ``POST /api/ingest/runs`` with ``kind = metadata``;
- ``tick``: ``POST /api/ticks`` for the fire's date;
- ``health``: ``POST /api/health/run`` (checks plus the operational
  halt), alerting here when unhealthy;
- ``report``: reads only the state DB, so it runs in this process;
- ``model_retrain``: ``POST /api/model-versions/retrain`` for the fire's
  date (roadmap 22.6).

The closed-day check uses ticker suffixes for calendars (``.CC`` is
crypto, 24/7), since the lake's asset classes are out of reach here.

:class:`~stonks.scheduling.in_process.InProcessExecutor` runs the same
jobs on the JobRunner inside ``stonks serve``; both share the job-result
interpretation in :func:`tick_job_outcome` and :func:`health_view_outcome`.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from typing import Any

from stonks.scheduling.api_client import SchedulerApiClient
from stonks.scheduling.jobs import (
    ActionRegistry,
    JobExecutor,
    JobOutcome,
    MembersResolver,
    RunContext,
    closed_day_outcome,
    ensure_window,
    job_is_scoped,
    job_universe,
    retrain_body,
    retrain_outcome,
    universes_outcome,
)

API_ACTIONS = ActionRegistry("api")

TERMINAL_JOB_STATUSES = frozenset({"succeeded", "failed", "cancelled"})

#: Substring of the tick's refusal to trade a date older than its latest
#: snapshot (``BackdatedTickError``): a late catch-up, not a failure.
_BACKDATED = "refusing to trade as_of"


class JobWaitTimeoutError(TimeoutError):
    pass


def tick_job_outcome(
    job_status: str, job_error: str | None, result: Mapping[str, Any] | None, job_id: str
) -> JobOutcome:
    """Interpret a finished tick job (API or JobRunner).

    A failed tick raised inside ``run_tick``, which already alerted;
    requests rejected before the tick started (backdated, bad universe)
    did not."""
    if job_status == "succeeded" and result is not None:
        detail = {
            "job_id": job_id,
            "tick_id": result.get("tick_id"),
            "tick_status": result.get("status"),
            "orders_placed": result.get("orders_placed"),
            "fills": result.get("fills"),
        }
        return JobOutcome("failed" if result.get("status") == "error" else "succeeded", detail)
    error = job_error or f"tick job {job_status}"
    if _BACKDATED in error:
        return JobOutcome("skipped", {"reason": "backdated", "job_id": job_id, "error": error})
    pre_tick = error.startswith(("ValidationError", "ConflictError", "NotFoundError"))
    return JobOutcome("failed", {"job_id": job_id, "error": error}, alerted=not pre_tick)


def ingest_job_outcome(
    job_status: str, job_error: str | None, result: Mapping[str, Any] | None, job_id: str
) -> JobOutcome:
    if job_status == "succeeded" and result is not None:
        detail = {
            "job_id": job_id,
            "ingest_run_id": result.get("run_id"),
            "ingest_status": result.get("status"),
            "tickers_ok": result.get("tickers_ok"),
            "tickers_failed": result.get("tickers_failed"),
        }
        return JobOutcome("failed" if result.get("status") == "error" else "succeeded", detail)
    return JobOutcome(
        "failed", {"job_id": job_id, "error": job_error or f"ingest job {job_status}"}
    )


def backup_job_outcome(
    job_status: str, job_error: str | None, result: Mapping[str, Any] | None, job_id: str
) -> JobOutcome:
    if job_status == "succeeded" and result is not None:
        return JobOutcome(
            "succeeded",
            {
                "job_id": job_id,
                "backup_id": result.get("backup_id"),
                "pruned": result.get("pruned"),
            },
        )
    return JobOutcome(
        "failed", {"job_id": job_id, "error": job_error or f"backup job {job_status}"}
    )


def retrain_job_outcome(
    job_status: str, job_error: str | None, result: Mapping[str, Any] | None, job_id: str
) -> JobOutcome:
    if job_status == "succeeded" and result is not None:
        return retrain_outcome(result, job_id)
    return JobOutcome(
        "failed", {"job_id": job_id, "error": job_error or f"retrain job {job_status}"}
    )


def health_view_outcome(ctx: RunContext, view: Mapping[str, Any]) -> JobOutcome:
    """A ``HealthReportView`` (as JSON) through the usual health alert."""
    from stonks.production.health import HealthCheck, HealthReport
    from stonks.scheduling.local import health_outcome

    checked_at = view.get("checked_at")
    report = HealthReport(
        checks=[HealthCheck(c["name"], bool(c["ok"]), c.get("detail", "")) for c in view["checks"]],
        checked_at=datetime.fromisoformat(checked_at)
        if isinstance(checked_at, str)
        else (checked_at or ctx.now),
    )
    return health_outcome(ctx, report)


def ingest_window(ctx: RunContext) -> tuple[str, str]:
    lookback = int(ctx.params.get("lookback_days", 7))
    return (ctx.fire.as_of - timedelta(days=lookback)).isoformat(), ctx.fire.as_of.isoformat()


class ApiExecutor(JobExecutor):
    backend = "api"

    def __init__(
        self,
        client: SchedulerApiClient,
        *,
        poll_seconds: float = 2.0,
        timeout_seconds: float = 3 * 3600,
        sleep: Callable[[float], object] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        stop: threading.Event | None = None,
    ) -> None:
        self.client = client
        self._poll = poll_seconds
        self._timeout = timeout_seconds
        self._sleep = sleep
        self._monotonic = monotonic
        self._stop = stop

    def actions(self) -> set[str]:
        return set(API_ACTIONS.names())

    def execute(self, ctx: RunContext) -> JobOutcome:
        return API_ACTIONS.get(ctx.spec.action)(ctx)

    def close(self) -> None:
        self.client.close()

    def bind_stop(self, stop: threading.Event) -> None:
        self._stop = stop

    def wait_for_job(self, job_id: str) -> dict[str, Any]:
        """Poll ``GET /api/jobs/{id}`` until the job is terminal."""
        deadline = self._monotonic() + self._timeout
        while True:
            job = self.client.get(f"/api/jobs/{job_id}")
            if job["status"] in TERMINAL_JOB_STATUSES:
                return job
            if self._stop is not None and self._stop.is_set():
                raise JobWaitTimeoutError(
                    f"scheduler stopping; job {job_id} keeps running in the API"
                )
            if self._monotonic() >= deadline:
                raise JobWaitTimeoutError(f"job {job_id} still {job['status']} after timeout")
            self._sleep(self._poll)


def _executor(ctx: RunContext) -> ApiExecutor:
    executor = ctx.executor
    if not isinstance(executor, ApiExecutor):  # pragma: no cover - wiring error
        raise TypeError("api actions need an ApiExecutor")
    return executor


def _members(ex: ApiExecutor) -> MembersResolver:
    """Reads a stored universe's members on a day from the API."""

    def members(universe_id: str, day: Any) -> list[str]:
        view = ex.client.get(f"/api/universes/{universe_id}/members", {"as_of": day.isoformat()})
        return [str(t) for t in view["tickers"]]

    return members


def _run_job(
    ex: ApiExecutor, start_path: str, body: dict[str, Any], result_path: str
) -> tuple[str, str, str | None, dict[str, Any] | None]:
    job = ex.client.post(start_path, body)
    job_id = job["id"]
    final = ex.wait_for_job(job_id)
    result = None
    if final["status"] == "succeeded":
        result = ex.client.get(result_path.format(job_id=job_id))
    return job_id, final["status"], final.get("error"), result


@API_ACTIONS.register("ingest_prices")
def api_ingest_prices(ctx: RunContext) -> JobOutcome:
    ex = _executor(ctx)
    universe = job_universe(ctx, _members(ex))
    if not universe:
        return JobOutcome("skipped", {"reason": "empty_universe"})
    closed = closed_day_outcome(ctx, universe, {})
    if closed is not None:
        return closed
    since, until = ingest_window(ctx)
    body = {
        "kind": "prices",
        "source": str(ctx.params.get("source", "eodhd")),
        "tickers": universe,
        "since": since,
        "until": until,
    }
    job_id, status, error, result = _run_job(
        ex, "/api/ingest/runs", body, "/api/ingest/jobs/{job_id}/result"
    )
    return ingest_job_outcome(status, error, result, job_id)


@API_ACTIONS.register("ingest_metadata")
def api_ingest_metadata(ctx: RunContext) -> JobOutcome:
    ex = _executor(ctx)
    universe = job_universe(ctx, _members(ex))
    if not universe:
        return JobOutcome("skipped", {"reason": "empty_universe"})
    closed = closed_day_outcome(ctx, universe, {})
    if closed is not None:
        return closed
    body = {
        "kind": "metadata",
        "source": str(ctx.params.get("source", "eodhd")),
        "tickers": universe,
    }
    job_id, status, error, result = _run_job(
        ex, "/api/ingest/runs", body, "/api/ingest/jobs/{job_id}/result"
    )
    return ingest_job_outcome(status, error, result, job_id)


@API_ACTIONS.register("tick")
def api_tick(ctx: RunContext) -> JobOutcome:
    ex = _executor(ctx)
    universe = job_universe(ctx, _members(ex))
    if not universe:
        return JobOutcome("skipped", {"reason": "empty_universe"})
    closed = closed_day_outcome(ctx, universe, {})
    if closed is not None:
        return closed
    body = {
        "as_of": ctx.fire.as_of.isoformat(),
        "tickers": universe,
        "scoped": job_is_scoped(ctx),
        "bars_due_at": ctx.fire.scheduled_for.isoformat(),
        "dry_run": bool(ctx.params.get("dry_run", False)),
    }
    job_id, status, error, result = _run_job(
        ex, "/api/ticks", body, "/api/ticks/jobs/{job_id}/result"
    )
    return tick_job_outcome(status, error, result, job_id)


@API_ACTIONS.register("health")
def api_health(ctx: RunContext) -> JobOutcome:
    ex = _executor(ctx)
    view = ex.client.post("/api/health/run", {"tickers": job_universe(ctx, _members(ex))})
    return health_view_outcome(ctx, view)


@API_ACTIONS.register("report")
def api_report(ctx: RunContext) -> JobOutcome:
    from stonks.scheduling.local import report_action

    return report_action(ctx)


@API_ACTIONS.register("backup")
def api_backup(ctx: RunContext) -> JobOutcome:
    """The server holds the lake, so it takes the backup (``POST /api/backups``)."""
    job_id, status, error, result = _run_job(
        _executor(ctx), "/api/backups", {}, "/api/backups/jobs/{job_id}/result"
    )
    return backup_job_outcome(status, error, result, job_id)


@API_ACTIONS.register("price_alerts")
def api_price_alerts(ctx: RunContext) -> JobOutcome:
    """The server holds the lake, so it checks the rules
    (``POST /api/price-alerts/evaluate``)."""
    view = _executor(ctx).client.post(
        "/api/price-alerts/evaluate", {"as_of": ctx.fire.as_of.isoformat()}
    )
    keys = ("rules", "checked", "fired", "published", "skipped_no_price")
    return JobOutcome("succeeded", {k: view.get(k) for k in keys})


@API_ACTIONS.register("model_retrain")
def api_model_retrain(ctx: RunContext) -> JobOutcome:
    """The server holds the lake, so it refits (``POST /api/model-versions/retrain``)."""
    job_id, status, error, result = _run_job(
        _executor(ctx),
        "/api/model-versions/retrain",
        retrain_body(ctx),
        "/api/model-versions/jobs/{job_id}/result",
    )
    return retrain_job_outcome(status, error, result, job_id)


@API_ACTIONS.register("connections_sync")
def api_connections_sync(ctx: RunContext) -> JobOutcome:
    from stonks.scheduling.local import connections_sync_action

    return connections_sync_action(ctx)


@API_ACTIONS.register("broker_health")
def api_broker_health(ctx: RunContext) -> JobOutcome:
    """State DB and the gateway socket only: runs in this process."""
    from stonks.scheduling.local import broker_health_action

    return broker_health_action(ctx)


@API_ACTIONS.register("ibkr_reauth_reminder")
def api_ibkr_reauth_reminder(ctx: RunContext) -> JobOutcome:
    from stonks.scheduling.local import ibkr_reauth_reminder_action

    return ibkr_reauth_reminder_action(ctx)


def ensure_body(ctx: RunContext) -> dict[str, Any]:
    """The ensure request of the ``universes_refresh`` job."""
    start, end = ensure_window(ctx)
    body: dict[str, Any] = {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "interval": str(ctx.params.get("interval", "1d")),
    }
    if ctx.params.get("source"):
        body["source"] = str(ctx.params["source"])
    return body


def ensure_step(status: str, error: str | None, result: Mapping[str, Any] | None) -> dict[str, Any]:
    step: dict[str, Any] = {"ensure": status}
    if result is not None:
        step |= {k: result.get(k) for k in ("tickers_fetched", "tickers_failed", "warnings")}
    if error:
        step["ensure_error"] = error
    return step


@API_ACTIONS.register("universes_refresh")
def api_universes_refresh(ctx: RunContext) -> JobOutcome:
    """Refresh every stored universe, then fetch its members' missing bars
    over the trailing ``ensure_days`` (both as lake-writer jobs in the API)."""
    ex = _executor(ctx)
    results: dict[str, dict[str, Any]] = {}
    for universe in ex.client.get_all("/api/universes"):
        uid = universe["id"]
        _, status, error, result = _run_job(
            ex, f"/api/universes/{uid}/refresh", {}, "/api/universes/refresh/{job_id}/result"
        )
        step: dict[str, Any] = {"refresh": status}
        if error:
            step["refresh_error"] = error
        if result is not None:
            step["members"] = result.get("members")
        if status == "succeeded" and ctx.params.get("ensure", True):
            _, e_status, e_error, e_result = _run_job(
                ex,
                f"/api/universes/{uid}/ensure",
                ensure_body(ctx),
                "/api/universes/ensure/{job_id}/result",
            )
            step |= ensure_step(e_status, e_error, e_result)
        results[uid] = step
    return universes_outcome(results)


# ---- calendars (roadmap 20.7) ----------------------------------------------------


def calendar_refresh_body(ctx: RunContext) -> dict[str, Any]:
    """The ``calendars_refresh`` job's request: the calendars from
    ``days_back`` (default 7) before the fire's date to ``days_ahead``
    (default 35) after it, for the whole market unless ``params.tickers``
    names some, then the upcoming-event notifications."""
    day = ctx.fire.as_of
    body: dict[str, Any] = {
        "start": (day - timedelta(days=int(ctx.params.get("days_back", 7)))).isoformat(),
        "end": (day + timedelta(days=int(ctx.params.get("days_ahead", 35)))).isoformat(),
        "source": str(ctx.params.get("source", "eodhd")),
        "alerts": bool(ctx.params.get("alerts", True)),
    }
    for key in ("tickers", "countries", "kinds", "alert_days"):
        if ctx.params.get(key):
            body[key] = ctx.params[key]
    return body


def calendar_job_outcome(
    status: str, error: str | None, result: Mapping[str, Any] | None, job_id: str
) -> JobOutcome:
    detail: dict[str, Any] = {"job_id": job_id, "job_status": status}
    if error:
        detail["error"] = error
    if result is not None:
        detail |= {
            k: result.get(k) for k in ("run_id", "status", "calendars_ok", "calendars_failed")
        }
        if result.get("alerts"):
            detail["alerts_sent"] = result["alerts"].get("sent")
    ok = status == "succeeded" and (result or {}).get("status") != "error"
    return JobOutcome("succeeded" if ok else "failed", detail)


@API_ACTIONS.register("calendars_refresh")
def api_calendars_refresh(ctx: RunContext) -> JobOutcome:
    """Refresh the event calendars through the API (a lake-writer job)."""
    job_id, status, error, result = _run_job(
        _executor(ctx),
        "/api/calendars/refresh",
        calendar_refresh_body(ctx),
        "/api/calendars/refresh/{job_id}/result",
    )
    return calendar_job_outcome(status, error, result, job_id)

"""``[scheduler]`` settings (roadmap 12.2-12.3).

``Settings.scheduler`` (the ``[scheduler]`` table).

Example::

    [scheduler]
    catch_up = "latest"          # none | latest | all
    max_catch_up_runs = 3
    catch_up_window_hours = 72

    [[scheduler.jobs]]
    name = "tick"
    action = "tick"
    deadline_minutes = 60
    ping_url_env = "STONKS_PING_TICK"   # healthchecks.io-style URL, kept in .env
    trigger = { type = "session", calendar = "XNYS", anchor = "close", offset_minutes = 45 }

Listing ``[[scheduler.jobs]]`` replaces the default job list entirely.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from datetime import time
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

CatchUpPolicy = Literal["none", "latest", "all"]
SchedulerBackend = Literal["auto", "api", "in_process", "local"]
ResolvedBackend = Literal["api", "in_process", "local"]


class SessionTriggerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["session"] = "session"
    calendar: str = "XNYS"
    anchor: Literal["open", "close"] = "close"
    offset_minutes: int = 0


class DailyTriggerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["daily"] = "daily"
    at: time
    timezone: str = "UTC"
    #: 0 = Monday ... 6 = Sunday; omitted = every day.
    weekdays: list[Annotated[int, Field(ge=0, le=6)]] | None = None
    #: Only fire on this calendar's trading days.
    calendar: str | None = None


class IntervalTriggerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["interval"] = "interval"
    every_minutes: int = Field(gt=0)


TriggerConfig = Annotated[
    SessionTriggerConfig | DailyTriggerConfig | IntervalTriggerConfig,
    Field(discriminator="type"),
]


class JobConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    name: str = Field(min_length=1, pattern=r"^[A-Za-z0-9_.-]+$")
    #: A registered job action (``ingest_prices``, ``tick``, ``health``,
    #: ``report``, ``universes_refresh``, ``backup``, ``connections_sync``,
    #: ``price_alerts``).
    action: str
    trigger: TriggerConfig
    params: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    #: Alert when no successful (or skipped) run exists this many minutes
    #: after the scheduled time. None disables the watchdog for the job.
    deadline_minutes: int | None = Field(default=None, gt=0)
    #: Overrides ``[scheduler].catch_up`` for this job.
    catch_up: CatchUpPolicy | None = None
    #: Dead-man ping URL (healthchecks.io style). URLs embed a token, so
    #: prefer ``ping_url_env`` and keep the URL in ``.env``.
    ping_url: SecretStr | None = None
    ping_url_env: str | None = None


def default_jobs() -> list[JobConfig]:
    """The daily loop on the NYSE calendar: refresh stored universes and
    fill their recent bars, ingest metadata (splits, dividends) and prices,
    check price alerts, tick, report after the close; health
    every four hours; a backup every night; due broker syncs every hour.
    ``universes_refresh`` skips while no universe is stored. The IB Gateway
    jobs (``broker_health`` every 5 minutes, ``ibkr_reauth_reminder`` on
    Sunday at 18:00 New York time) skip while no gateway is configured."""
    return [
        JobConfig(
            name="universes_refresh",
            action="universes_refresh",
            trigger=SessionTriggerConfig(offset_minutes=20),
        ),
        # Splits and dividends for the tick (TO-05). The free EODHD plan
        # has no metadata endpoint, so it reads Yahoo; set params.source
        # to "eodhd" on a paid plan.
        JobConfig(
            name="ingest_metadata",
            action="ingest_metadata",
            trigger=SessionTriggerConfig(offset_minutes=25),
            params={"source": "yahoo"},
        ),
        JobConfig(
            name="ingest_prices",
            action="ingest_prices",
            trigger=SessionTriggerConfig(offset_minutes=30),
            deadline_minutes=60,
        ),
        # Price alerts on the closes the ingest just stored (roadmap 20.2).
        JobConfig(
            name="price_alerts",
            action="price_alerts",
            trigger=SessionTriggerConfig(offset_minutes=40),
        ),
        JobConfig(
            name="tick",
            action="tick",
            trigger=SessionTriggerConfig(offset_minutes=45),
            deadline_minutes=60,
        ),
        JobConfig(
            name="report",
            action="report",
            trigger=SessionTriggerConfig(offset_minutes=90),
        ),
        JobConfig(
            name="health",
            action="health",
            trigger=IntervalTriggerConfig(every_minutes=240),
        ),
        JobConfig(
            name="backup",
            action="backup",
            trigger=DailyTriggerConfig(at=time(5, 0)),
            deadline_minutes=120,
        ),
        JobConfig(
            name="connections_sync",
            action="connections_sync",
            trigger=IntervalTriggerConfig(every_minutes=60),
        ),
        JobConfig(
            name="broker_health",
            action="broker_health",
            trigger=IntervalTriggerConfig(every_minutes=5),
            catch_up="none",
        ),
        JobConfig(
            name="ibkr_reauth_reminder",
            action="ibkr_reauth_reminder",
            trigger=DailyTriggerConfig(at=time(18, 0), timezone="America/New_York", weekdays=[6]),
            catch_up="none",
        ),
    ]


class SchedulerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    #: Where jobs run. ``auto``: ``api`` when an API URL is configured
    #: (``api_url`` or ``STONKS_API_URL``), else ``local``. ``in_process``
    #: is what ``stonks serve`` uses when it hosts the scheduler itself.
    backend: SchedulerBackend = "auto"
    #: The running API (``api`` backend); ``STONKS_API_URL`` overrides it.
    #: The token is ``STONKS_API_TOKEN``, the one ``stonks serve`` uses.
    api_url: str | None = None
    #: Plain-http hosts the token may be sent to (e.g. ``["api"]`` on a
    #: private Compose network). https and loopback are always allowed.
    api_trusted_hosts: list[str] = Field(default_factory=list)
    api_timeout_seconds: float = Field(default=30.0, gt=0)
    #: How often a job started through the API or the JobRunner is polled.
    job_poll_seconds: float = Field(default=2.0, gt=0)
    #: Give up waiting (and fail the run) after this long.
    job_timeout_minutes: float = Field(default=180.0, gt=0)
    #: What to do with fires missed while the scheduler was down:
    #: ``none`` skips them, ``latest`` runs only the most recent one per
    #: job, ``all`` runs up to ``max_catch_up_runs`` of the most recent.
    catch_up: CatchUpPolicy = "latest"
    max_catch_up_runs: int = Field(default=3, ge=1)
    #: Fires older than this are never caught up.
    catch_up_window_hours: float = Field(default=72.0, gt=0)
    #: Longest sleep between wake-ups (the loop also wakes at each fire).
    poll_seconds: float = Field(default=60.0, gt=0)
    #: How often the deadline watchdog checks for missed runs.
    watchdog_seconds: float = Field(default=60.0, gt=0)
    #: Single-instance lock file; default ``scheduler.lock`` next to the state DB.
    lock_path: Path | None = None
    ping_timeout_seconds: float = Field(default=5.0, gt=0)
    #: Run the notification ``DeliveryWorker`` on its own thread next to
    #: the scheduler (standalone or inside ``stonks serve``).
    deliver_notifications: bool = True
    #: Sleep between delivery passes that found nothing to send.
    delivery_interval_seconds: float = Field(default=5.0, gt=0)
    jobs: list[JobConfig] = Field(default_factory=default_jobs)

    @model_validator(mode="after")
    def _unique_names(self) -> SchedulerConfig:
        names = [j.name for j in self.jobs]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise ValueError(f"duplicate scheduler job names: {dupes}")
        return self


API_URL_ENV = "STONKS_API_URL"
API_TOKEN_ENV = "STONKS_API_TOKEN"


def resolved_api_url(config: SchedulerConfig, env: Mapping[str, str]) -> str | None:
    return env.get(API_URL_ENV) or config.api_url


def resolve_backend(config: SchedulerConfig, env: Mapping[str, str]) -> ResolvedBackend:
    if config.backend != "auto":
        return config.backend
    return "api" if resolved_api_url(config, env) else "local"


def scheduler_config_from(settings: object, config_path: Path | None = None) -> SchedulerConfig:
    """``settings.scheduler``; for a settings object without it, the
    ``[scheduler]`` table of the TOML config (default when absent)."""
    existing = getattr(settings, "scheduler", None)
    if isinstance(existing, SchedulerConfig):
        return existing
    from stonks.config import DEFAULT_CONFIG_PATH

    path = Path(config_path or DEFAULT_CONFIG_PATH)
    if not path.exists():
        return SchedulerConfig()
    with open(path, "rb") as f:
        data = tomllib.load(f)
    return SchedulerConfig(**data.get("scheduler", {}))

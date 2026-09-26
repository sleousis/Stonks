"""``[scheduler]`` settings (roadmap 12.2-12.3).

A pydantic sub-model meant to become ``Settings.scheduler``. Until that
field exists, :func:`scheduler_config_from` reads the ``[scheduler]``
table straight from the TOML file ``load_settings`` uses.

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
from datetime import time
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

CatchUpPolicy = Literal["none", "latest", "all"]


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
    #: A registered job action (``ingest_prices``, ``tick``, ``health``, ``report``).
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
    """The daily loop on the NYSE calendar: ingest, tick, report after the
    close; health every four hours."""
    return [
        JobConfig(
            name="ingest_prices",
            action="ingest_prices",
            trigger=SessionTriggerConfig(offset_minutes=30),
            deadline_minutes=60,
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
    ]


class SchedulerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
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
    jobs: list[JobConfig] = Field(default_factory=default_jobs)

    @model_validator(mode="after")
    def _unique_names(self) -> SchedulerConfig:
        names = [j.name for j in self.jobs]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise ValueError(f"duplicate scheduler job names: {dupes}")
        return self


def scheduler_config_from(settings: object, config_path: Path | None = None) -> SchedulerConfig:
    """``settings.scheduler`` once the Settings field exists; until then
    the ``[scheduler]`` table of the TOML config (default when absent)."""
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

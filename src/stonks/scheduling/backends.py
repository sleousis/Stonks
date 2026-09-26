"""Picks the job executor for a standalone scheduler process."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from stonks.scheduling.config import (
    API_TOKEN_ENV,
    SchedulerConfig,
    resolve_backend,
    resolved_api_url,
)
from stonks.scheduling.jobs import JobExecutor


class BackendConfigError(ValueError):
    pass


#: Comma-separated plain-http hosts the token may go to, on top of
#: ``[scheduler].api_trusted_hosts`` (e.g. ``api`` on a Compose network).
API_TRUSTED_HOSTS_ENV = "STONKS_API_TRUSTED_HOSTS"


def _env_hosts(env: Mapping[str, str]) -> list[str]:
    return [h.strip() for h in env.get(API_TRUSTED_HOSTS_ENV, "").split(",") if h.strip()]


def build_executor(
    config: SchedulerConfig,
    env: Mapping[str, str] | None = None,
    *,
    transport: Any = None,
) -> JobExecutor:
    """``api`` (an API URL is configured) or ``local``. ``in_process``
    only exists inside ``stonks serve`` (see
    :func:`~stonks.scheduling.in_process.start_in_process_scheduler`)."""
    env = os.environ if env is None else env
    backend = resolve_backend(config, env)
    if backend == "local":
        from stonks.scheduling.local import LocalExecutor

        return LocalExecutor()
    if backend == "in_process":
        raise BackendConfigError(
            "backend 'in_process' runs inside `stonks serve`; a standalone scheduler "
            "uses 'api' (set STONKS_API_URL) or 'local'"
        )
    url = resolved_api_url(config, env)
    if not url:
        raise BackendConfigError(
            "backend 'api' needs the API URL: set STONKS_API_URL or [scheduler].api_url"
        )
    from stonks.scheduling.api_backend import ApiExecutor
    from stonks.scheduling.api_client import SchedulerApiClient

    client = SchedulerApiClient(
        url,
        token=env.get(API_TOKEN_ENV) or None,
        timeout=config.api_timeout_seconds,
        trusted_hosts=[*config.api_trusted_hosts, *_env_hosts(env)],
        transport=transport,
    )
    return ApiExecutor(
        client,
        poll_seconds=config.job_poll_seconds,
        timeout_seconds=config.job_timeout_minutes * 60,
    )

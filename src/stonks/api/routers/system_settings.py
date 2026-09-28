"""System settings an admin edits in the console (complexity audit F61):
risk limits, the trading universe, schedule switches and times, and
notification defaults. Stored as overrides on top of the TOML config, with
an audit row for every change. Secrets are never editable here."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path

from stonks.api.deps import PrincipalDep, ServicesDep, needs
from stonks.api.errors import PROBLEM_RESPONSES
from stonks.app.system_settings import (
    SystemSettingChange,
    SystemSettingReset,
    SystemSettingsService,
    SystemSettingsView,
    SystemSettingView,
)
from stonks.auth import Permission

router = APIRouter(
    prefix="/api/settings/system", tags=["system-settings"], responses=PROBLEM_RESPONSES
)

SettingKey = Annotated[str, Path(max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")]


def _service(services: ServicesDep) -> SystemSettingsService:
    return SystemSettingsService(services.context)


@router.get(
    "",
    response_model=SystemSettingsView,
    operation_id="listSystemSettings",
    dependencies=needs(Permission.SETTINGS_READ),
)
def list_system_settings(services: ServicesDep, principal: PrincipalDep) -> SystemSettingsView:
    """Every setting an admin may change here, with the value in effect, the
    TOML value, and who changed it last and why."""
    return _service(services).list(principal)


@router.get(
    "/{key}",
    response_model=SystemSettingView,
    operation_id="getSystemSetting",
    dependencies=needs(Permission.SETTINGS_READ),
)
def get_system_setting(
    key: SettingKey, services: ServicesDep, principal: PrincipalDep
) -> SystemSettingView:
    """One editable setting (404 for a key that is not editable)."""
    return _service(services).get(principal, key)


@router.put(
    "/{key}",
    response_model=SystemSettingView,
    operation_id="changeSystemSetting",
    dependencies=needs(Permission.SETTINGS_MANAGE),
)
def change_system_setting(
    key: SettingKey, body: SystemSettingChange, services: ServicesDep, principal: PrincipalDep
) -> SystemSettingView:
    """Change one setting. The value is validated like the TOML config (422
    when it would not load), stored with an audit row, and used from the
    next run on (``applies``). Needs a fresh second factor."""
    return _service(services).change(principal, key, body)


@router.post(
    "/{key}/reset",
    response_model=SystemSettingView,
    operation_id="resetSystemSetting",
    dependencies=needs(Permission.SETTINGS_MANAGE),
)
def reset_system_setting(
    key: SettingKey, body: SystemSettingReset, services: ServicesDep, principal: PrincipalDep
) -> SystemSettingView:
    """Drop the override, so the TOML value applies again. Audited."""
    return _service(services).reset(principal, key, body)

"""SystemSettingsService — the system settings an admin edits in the console
(complexity audit F61), over :mod:`stonks.config_overrides`.

Reads list every editable key with its effective value, the TOML value and
who changed it. A change is validated against the whole settings model
before it is stored, writes an ``audit_log`` row, and takes effect on the
next run of the blocks that read settings per run (``applies``). Secrets
are never in the catalog.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import BaseModel, Field, StringConstraints

from stonks.accounts import Scope
from stonks.app.context import AppContext
from stonks.app.errors import NotFoundError, ValidationError
from stonks.auth.errors import PermissionDenied
from stonks.auth.policy import Permission, require
from stonks.auth.principal import Principal
from stonks.config_overrides import (
    Applies,
    EditableSetting,
    Group,
    NotEditable,
    OverrideRow,
    OverrideStore,
    apply_override,
    apply_overrides,
    editable_settings,
    find_setting,
    read_value,
)

#: The caller: a :class:`Principal` (API) or the shell's service
#: :class:`Scope` (``stonks settings``, the operator is an admin).
Who = Principal | Scope

Reason = Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=500)]


class SystemSettingView(BaseModel):
    key: str = Field(
        description="Dotted path in the config, e.g. production.risk.max_weight_per_ticker."
    )
    group: Group
    label: str
    help: str
    applies: Applies = Field(
        description="next_run: the next trading run or job uses it. restart: the scheduler "
        "picks it up when it restarts."
    )
    value: Any = Field(description="The value in effect now.")
    default: Any = Field(description="The value from the TOML config and environment.")
    overridden: bool
    updated_at: str | None = None
    updated_by: str | None = None
    reason: str | None = None
    problem: str | None = Field(
        default=None,
        description="Set when the stored override no longer validates and is skipped.",
    )


class SystemSettingsView(BaseModel):
    items: list[SystemSettingView]


class SystemSettingChange(BaseModel):
    value: Any = Field(description="The new value; null turns an optional limit off.")
    reason: Reason = Field(description="Why, kept in the audit log.")


class SystemSettingReset(BaseModel):
    reason: Reason


class SystemSettingsService:
    def __init__(self, context: AppContext) -> None:
        self._ctx = context

    def list(self, principal: Who) -> SystemSettingsView:
        _actor(principal, Permission.SETTINGS_READ)
        base = self._ctx.base_settings
        with self._ctx.state() as state:
            rows = {r.key: r for r in OverrideStore(state).rows()}
        effective, problems = apply_overrides(base, {k: r.value for k, r in rows.items()})
        return SystemSettingsView(
            items=[
                _view(entry, base, effective, rows.get(entry.key), problems.get(entry.key))
                for entry in editable_settings(base)
            ]
        )

    def get(self, principal: Who, key: str) -> SystemSettingView:
        found = next((s for s in self.list(principal).items if s.key == key), None)
        if found is None:
            raise NotFoundError(f"{key!r} is not an editable setting")
        return found

    def change(self, principal: Who, key: str, body: SystemSettingChange) -> SystemSettingView:
        """Validate ``body.value`` for ``key`` on top of the other overrides,
        then store it with an audit row."""
        actor = _actor(principal, Permission.SETTINGS_MANAGE)
        base = self._ctx.base_settings
        if find_setting(base, key) is None:
            raise NotFoundError(f"{key!r} is not an editable setting")
        with self._ctx.state() as state:
            store = OverrideStore(state)
            others = {k: v for k, v in store.values().items() if k != key}
            current, _ = apply_overrides(base, others)
            try:
                apply_override(current, key, body.value)
            except NotEditable:
                raise NotFoundError(f"{key!r} is not an editable setting") from None
            except ValueError as exc:
                raise ValidationError(f"{key}: {_first_error(exc)}") from None
            store.set(key, body.value, actor=actor, reason=body.reason)
        self._ctx.invalidate_settings()
        return self.get(principal, key)

    def reset(self, principal: Who, key: str, body: SystemSettingReset) -> SystemSettingView:
        """Drop the override of ``key``: the TOML value applies again."""
        actor = _actor(principal, Permission.SETTINGS_MANAGE)
        if find_setting(self._ctx.base_settings, key) is None:
            raise NotFoundError(f"{key!r} is not an editable setting")
        with self._ctx.state() as state:
            OverrideStore(state).reset(key, actor=actor, reason=body.reason)
        self._ctx.invalidate_settings()
        return self.get(principal, key)


def _actor(who: Who, permission: Permission) -> str:
    """The audit actor, once ``who`` may use ``permission``."""
    if isinstance(who, Principal):
        require(who, permission)
        return who.actor
    if not who.is_service:
        raise PermissionDenied(f"{permission.value} is not allowed for this caller")
    return who.actor


def _view(
    entry: EditableSetting,
    base: Any,
    effective: Any,
    row: OverrideRow | None,
    problem: str | None,
) -> SystemSettingView:
    return SystemSettingView(
        key=entry.key,
        group=entry.group,
        label=entry.label,
        help=entry.help,
        applies=entry.applies,
        value=read_value(effective, entry.key),
        default=read_value(base, entry.key),
        overridden=row is not None and problem is None,
        updated_at=row.updated_at if row else None,
        updated_by=row.updated_by if row else None,
        reason=row.reason if row else None,
        problem=problem,
    )


def _first_error(exc: ValueError) -> str:
    errors = getattr(exc, "errors", None)
    if callable(errors):
        found = errors()
        if found:
            return str(found[0].get("msg", exc))
    return str(exc).splitlines()[0]

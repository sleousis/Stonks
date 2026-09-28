"""``stonks settings``: the system settings an admin may change in the
console (risk limits, universe, schedule, notification defaults), stored as
overrides on top of the TOML config. Runs as the operator
(``service:cli``); every change is audited."""

from __future__ import annotations

import json
from typing import Any

import typer
from rich.console import Console

app = typer.Typer(help="System settings the admin console can change", no_args_is_help=True)

_REASON = typer.Option(..., "--reason", help="why, kept in the audit log")


def _service() -> Any:
    from stonks.app.context import AppContext
    from stonks.app.system_settings import SystemSettingsService
    from stonks.cli import _settings

    context = AppContext(_settings())
    with context.state() as state:
        state.migrate()
    return SystemSettingsService(context)


def _operator() -> Any:
    from stonks.accounts import Scope

    return Scope.service("cli")


def _call(fn: Any) -> Any:
    from stonks.app.errors import AppError
    from stonks.auth.errors import PermissionDenied

    try:
        return fn()
    except (AppError, PermissionDenied, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from None


def _parse(raw: str) -> Any:
    """JSON when it parses (numbers, true, null, lists), else the text."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


@app.command("list")
def list_settings(group: str | None = typer.Option(None, "--group")) -> None:
    """Every editable setting: the value in effect and the TOML value."""
    view = _call(lambda: _service().list(_operator()))
    for s in view.items:
        if group and s.group != group:
            continue
        line = f"{s.key} = {json.dumps(s.value)}  [{s.group}, applies {s.applies}]"
        if s.overridden:
            line += f"  (file: {json.dumps(s.default)}; by {s.updated_by}: {s.reason})"
        if s.problem:
            line += f"  (skipped: {s.problem})"
        typer.echo(line)


@app.command("set")
def set_setting(key: str, value: str, reason: str = _REASON) -> None:
    """Set KEY to VALUE (JSON, e.g. 0.2, null, '["SPY.US"]')."""
    from stonks.app.system_settings import SystemSettingChange

    body = SystemSettingChange(value=_parse(value), reason=reason)
    view = _call(lambda: _service().change(_operator(), key, body))
    Console().print(f"{view.key} = {json.dumps(view.value)} (applies: {view.applies})")


@app.command("reset")
def reset_setting(key: str, reason: str = _REASON) -> None:
    """Drop the override of KEY, so the TOML value applies again."""
    from stonks.app.system_settings import SystemSettingReset

    view = _call(lambda: _service().reset(_operator(), key, SystemSettingReset(reason=reason)))
    Console().print(f"{view.key} = {json.dumps(view.value)} (from the config file)")

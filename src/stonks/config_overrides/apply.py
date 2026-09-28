"""Lay catalog overrides over a ``Settings`` object with full validation.

Each override rebuilds only its top-level section (``production``,
``notify``, ``scheduler``) through that section's own pydantic model, then
swaps it in with ``model_copy``. The base object is never mutated, and the
other sections are shared as they are.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel

from stonks.config_overrides.catalog import find_setting


class NotEditable(KeyError):
    """The key is not in the catalog (a secret, a path, or unknown)."""


def _navigate(node: Any, part: str) -> Any:
    if isinstance(node, list):
        for item in node:
            name = item.get("name") if isinstance(item, dict) else getattr(item, "name", None)
            if name == part:
                return item
        raise NotEditable(part)
    if isinstance(node, dict):
        if part not in node:
            raise NotEditable(part)
        return node[part]
    if isinstance(node, BaseModel):
        if part not in type(node).model_fields:
            raise NotEditable(part)
        return getattr(node, part)
    raise NotEditable(part)


def read_value(settings: Any, key: str) -> Any:
    """The current value of ``key`` in ``settings`` (JSON-ready)."""
    node: Any = settings
    for part in key.split("."):
        node = _navigate(node, part)
    if isinstance(node, BaseModel):
        return node.model_dump(mode="json")
    if isinstance(node, tuple):
        return [list(v) if isinstance(v, tuple) else v for v in node]
    if hasattr(node, "isoformat"):
        return node.isoformat(timespec="minutes") if "time" in type(node).__name__ else str(node)
    return node


def apply_override(settings: Any, key: str, value: Any) -> Any:
    """``settings`` with ``key`` set to ``value``. :class:`NotEditable` for a
    key outside the catalog; pydantic's ``ValidationError`` for a bad value."""
    if find_setting(settings, key) is None:
        raise NotEditable(key)
    section, *path = key.split(".")
    current = getattr(settings, section)
    data = current.model_dump()
    node: Any = data
    for part in path[:-1]:
        node = _navigate(node, part)
    if not isinstance(node, dict) or path[-1] not in node:
        raise NotEditable(key)
    node[path[-1]] = value
    rebuilt = type(current).model_validate(data)
    return settings.model_copy(update={section: rebuilt})


def apply_overrides(settings: Any, overrides: Mapping[str, Any]) -> tuple[Any, dict[str, str]]:
    """``settings`` with every override that still validates, and the ones
    skipped with why (a stored override a newer release no longer takes)."""
    out = settings
    problems: dict[str, str] = {}
    for key in sorted(overrides):
        try:
            out = apply_override(out, key, overrides[key])
        except (NotEditable, ValueError) as exc:  # pydantic's ValidationError is a ValueError
            problems[key] = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
    return out, problems


def with_overrides(settings: Any) -> Any:
    """``settings`` with the overrides stored in its state DB (for entry
    points that load settings once: the CLI and the scheduler process)."""
    from stonks.config_overrides.store import load_override_values

    values = load_override_values(settings.state.path)
    return apply_overrides(settings, values)[0] if values else settings

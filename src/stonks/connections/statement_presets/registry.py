"""Preset registry. A preset is one module in
``stonks.connections.statement_presets`` whose class is decorated with
:func:`register_preset`; adding one never edits a list."""

from __future__ import annotations

import importlib
import pkgutil
import threading
from collections.abc import Sequence

from stonks.connections.statement_csv import StatementError
from stonks.connections.statement_presets.base import StatementPreset

_PRESETS: dict[str, StatementPreset] = {}
_LOCK = threading.Lock()
_DISCOVERED = False
_INFRA = frozenset({"base", "registry", "lake_resolver"})


def register_preset(cls: type[StatementPreset]) -> type[StatementPreset]:
    name = cls.id
    if not name or name != name.lower() or not name.replace("_", "").isalnum():
        raise ValueError(f"bad preset id {name!r}")
    existing = _PRESETS.get(name)
    if existing is not None and type(existing).__qualname__ != cls.__qualname__:
        raise ValueError(f"preset {name!r} is registered twice")
    _PRESETS[name] = cls()
    return cls


def _discover() -> None:
    global _DISCOVERED
    with _LOCK:
        if _DISCOVERED:
            return
        import stonks.connections.statement_presets as package

        for info in pkgutil.iter_modules(package.__path__):
            if not info.name.startswith("_") and info.name not in _INFRA:
                importlib.import_module(f"{package.__name__}.{info.name}")
        _DISCOVERED = True


def presets() -> list[StatementPreset]:
    """Every preset, by broker then id."""
    _discover()
    return sorted(_PRESETS.values(), key=lambda p: (p.broker.lower(), p.id))


def preset(preset_id: str) -> StatementPreset:
    _discover()
    found = _PRESETS.get(preset_id)
    if found is None:
        known = ", ".join(sorted(_PRESETS))
        raise StatementError(f"no preset named {preset_id!r}; known: {known}")
    return found


def detect(headers: Sequence[str]) -> tuple[StatementPreset, str] | None:
    """The preset whose export has these headers, with the file's locale."""
    for p in presets():
        locale = p.detect(headers)
        if locale is not None:
            return p, locale
    return None

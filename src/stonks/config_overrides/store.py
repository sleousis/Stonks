"""``settings_overrides`` in the state DB, with an ``audit_log`` row for
every change (written in the same transaction)."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from stonks.accounts.audit import AuditLog, iso_now
from stonks.store.state import SqliteState


@dataclass(frozen=True)
class OverrideRow:
    key: str
    value: Any
    updated_at: str
    updated_by: str
    reason: str


class OverrideStore:
    def __init__(self, state: SqliteState) -> None:
        self._state = state

    def rows(self) -> list[OverrideRow]:
        try:
            found = self._state.sql("SELECT * FROM settings_overrides ORDER BY key")
        except sqlite3.OperationalError:  # before migration 048
            return []
        return [
            OverrideRow(
                key=r["key"],
                value=json.loads(r["value_json"]),
                updated_at=r["updated_at"],
                updated_by=r["updated_by"],
                reason=r["reason"],
            )
            for r in found
        ]

    def values(self) -> dict[str, Any]:
        return {r.key: r.value for r in self.rows()}

    def set(self, key: str, value: Any, *, actor: str, reason: str) -> None:
        """Store ``value`` for ``key`` (validated by the caller)."""
        previous = self.values().get(key)
        with self._state.transaction():
            self._state.execute(
                "INSERT INTO settings_overrides (key, value_json, updated_at, updated_by, reason)"
                " VALUES (?, ?, ?, ?, ?) ON CONFLICT (key) DO UPDATE SET"
                " value_json = excluded.value_json, updated_at = excluded.updated_at,"
                " updated_by = excluded.updated_by, reason = excluded.reason",
                [key, json.dumps(value), iso_now(), actor, reason],
            )
            AuditLog(self._state).record(
                actor,
                "settings.override",
                "setting",
                key,
                details={"from": previous, "to": value, "reason": reason},
            )

    def reset(self, key: str, *, actor: str, reason: str) -> bool:
        """Drop the override of ``key`` (back to the TOML value). ``False``
        when there was none."""
        previous = self.values()
        if key not in previous:
            return False
        with self._state.transaction():
            self._state.execute("DELETE FROM settings_overrides WHERE key = ?", [key])
            AuditLog(self._state).record(
                actor,
                "settings.reset",
                "setting",
                key,
                details={"from": previous[key], "reason": reason},
            )
        return True


def load_override_values(state_path: Path | str) -> dict[str, Any]:
    """The stored overrides, or none when the state file does not exist yet
    (never creates it)."""
    path = Path(state_path)
    if not path.exists():
        return {}
    with SqliteState(path) as state:
        return OverrideStore(state).values()

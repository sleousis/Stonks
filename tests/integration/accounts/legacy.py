"""Build a state database as it was before the accounts migration (010), to
test the upgrade path of an existing single-owner install."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from stonks.store import state as state_mod
from stonks.store.state import SqliteState

#: Version of ``010_accounts.sql``.
ACCOUNTS_VERSION = 10


def legacy_migrations_dir(dest: Path) -> Path:
    """A copy of the migrations older than the accounts one."""
    dest.mkdir(parents=True, exist_ok=True)
    for path in state_mod.MIGRATIONS_DIR.glob("*.sql"):
        if int(path.stem.split("_", 1)[0]) < ACCOUNTS_VERSION:
            shutil.copy(path, dest / path.name)
    return dest


def legacy_state(path: Path, monkeypatch: pytest.MonkeyPatch) -> SqliteState:
    """A state DB migrated only up to 009 (the patch is undone right away)."""
    real = state_mod.MIGRATIONS_DIR
    monkeypatch.setattr(state_mod, "MIGRATIONS_DIR", legacy_migrations_dir(path.parent / "_mig"))
    state = SqliteState(path)
    state.migrate()
    monkeypatch.setattr(state_mod, "MIGRATIONS_DIR", real)
    assert ACCOUNTS_VERSION not in state.applied_migrations()
    return state


def migrate_to_current(state: SqliteState) -> None:
    state.migrate()
    assert ACCOUNTS_VERSION in state.applied_migrations()

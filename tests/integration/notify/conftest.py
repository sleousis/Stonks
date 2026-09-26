"""Shared fixtures: a migrated state DB with a few users."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from stonks.accounts import Role, Scope, UserRepository
from stonks.store.state import SqliteState

NOW = datetime(2026, 1, 5, 15, 0, tzinfo=UTC)  # a Monday, 10:00 in New York


class Clock:
    """A settable clock for the router, outbox and worker."""

    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def state(tmp_path):
    s = SqliteState(tmp_path / "state.sqlite")
    s.migrate()
    yield s
    s.close()


@pytest.fixture
def users(state):
    repo = UserRepository(state)
    alice = repo.create(
        display_name="Alice", role=Role.TRADER, actor="service:system", email="alice@example.com"
    )
    bob = repo.create(display_name="Bob", role=Role.TRADER, actor="service:system")
    admin = repo.create(
        display_name="Ada", role=Role.ADMIN, actor="service:system", email="ada@example.com"
    )
    return {"alice": alice, "bob": bob, "admin": admin}


@pytest.fixture
def alice_scope(users) -> Scope:
    return Scope.for_user(users["alice"])


@pytest.fixture
def bob_scope(users) -> Scope:
    return Scope.for_user(users["bob"])

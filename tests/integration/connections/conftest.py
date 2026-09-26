"""Shared fixtures: a migrated state DB, two traders, a SecretBox and a
connections service with the fake providers enabled."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from stonks.accounts import Role, Scope, UserRepository
from stonks.connections.providers import fake
from stonks.connections.ratelimit import reset_limiters
from stonks.connections.service import ConnectionService
from stonks.connections.settings import ConnectionsConfig
from stonks.security import KeyRing, SecretBox, generate_key
from stonks.store.state import SqliteState

ADMIN = Scope.service("system")


class Clock:
    def __init__(self) -> None:
        # A Tuesday, 11:00 in New York: market hours.
        self.now = datetime(2026, 3, 3, 16, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture(autouse=True)
def _clean_fakes():
    fake.FAKE_BOOKS.clear()
    reset_limiters()
    yield
    fake.FAKE_BOOKS.clear()
    reset_limiters()


@pytest.fixture
def state(tmp_path):
    s = SqliteState(tmp_path / "state.sqlite")
    s.migrate()
    yield s
    s.close()


@pytest.fixture
def alice(state) -> Scope:
    user = UserRepository(state).create(display_name="Alice", role=Role.TRADER, actor="t")
    return Scope.for_user(user)


@pytest.fixture
def bob(state) -> Scope:
    user = UserRepository(state).create(display_name="Bob", role=Role.TRADER, actor="t")
    return Scope.for_user(user)


@pytest.fixture
def key() -> str:
    return generate_key()


@pytest.fixture
def box(key) -> SecretBox:
    return SecretBox(KeyRing.parse(f"k1:{key}"))


@pytest.fixture
def config() -> ConnectionsConfig:
    return ConnectionsConfig(enabled_providers=("fake", "fake_portal"))


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def service(state, config, box, clock) -> ConnectionService:
    return ConnectionService(state, config, box=box, clock=clock)

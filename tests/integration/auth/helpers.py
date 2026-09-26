"""Shared set-up for auth tests: a migrated state file, a cheap password
hasher, a fixed encryption key and a clock the test can move."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pyotp

from stonks.accounts import Role, UserRepository
from stonks.auth import AuthService, AuthSettings, PasswordHasher, Principal
from stonks.auth.principal import ROLE_SCOPES
from stonks.security import KeyRing, SecretBox, generate_key
from stonks.store.state import SqliteState

FAST_HASHER = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
PASSWORD = "correct horse battery staple"
START = datetime(2026, 9, 26, 12, 0, 0, tzinfo=UTC)


class Clock:
    def __init__(self, now: datetime = START) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


def make_service(
    path,
    *,
    clock: Callable[[], datetime] | None = None,
    legacy: str | None = None,
    box: SecretBox | None = None,
) -> AuthService:
    @contextmanager
    def factory():
        with SqliteState(path) as state:
            yield state

    return AuthService(
        factory,
        settings=AuthSettings(),
        hasher=FAST_HASHER,
        box=box or SecretBox(KeyRing.parse(f"k1:{generate_key()}")),
        legacy_token=lambda: legacy,
        clock=clock or Clock(),
    )


def add_user(path, email: str, role: Role = Role.TRADER, password: str = PASSWORD) -> str:
    with SqliteState(path) as state:
        user = UserRepository(state).create(
            display_name=email.split("@")[0], role=role, actor="service:test", email=email
        )
        state.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            [FAST_HASHER.hash(password), user.id],
        )
    return user.id


def enrol(svc: AuthService, email: str, clock: Clock, password: str = PASSWORD):
    """Password, TOTP enrolment; returns (full session, totp secret, recovery codes)."""
    login = svc.login(email, password, ip="10.0.0.1")
    info = svc.session(login.session.token, unsafe=False)
    start = svc.enrol_start(info)
    result = svc.enrol_confirm(info, pyotp.TOTP(start.secret).at(clock()))
    assert result.session is not None
    return result.session, start.secret, result.recovery_codes


def session_principal(user_id: str, role: Role, *, mfa_fresh: bool = True) -> Principal:
    return Principal.create(
        user_id=user_id,
        kind="human",
        role=role,
        scopes=ROLE_SCOPES[role],
        mfa_fresh=mfa_fresh,
        via="session",
    )

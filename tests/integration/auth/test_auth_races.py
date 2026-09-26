"""Review findings AS-11 and AS-12: the login limit and the one-time TOTP
check hold under concurrent requests, and admin resets revoke API tokens."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pyotp
import pytest

from stonks.accounts import DEFAULT_OWNER_ID, Role
from stonks.auth import InvalidCredentials, NotAuthenticated, TooManyAttempts
from stonks.auth.passwords import PasswordHasher
from stonks.store.state import SqliteState
from tests.integration.auth.helpers import (
    FAST_HASHER,
    PASSWORD,
    Clock,
    add_user,
    enrol,
    make_service,
    session_principal,
)


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "state.sqlite"
    with SqliteState(path) as state:
        state.migrate()
    return path


class _CountingHasher(PasswordHasher):
    """The fast test hasher, slowed a little so parallel logins overlap."""

    def __init__(self) -> None:
        super().__init__(time_cost=1, memory_cost=8, parallelism=1)
        self.calls = 0
        self._lock = threading.Lock()

    def verify(self, stored, password):
        with self._lock:
            self.calls += 1
        threading.Event().wait(0.05)
        return FAST_HASHER.verify(stored, password)


def test_as11_parallel_wrong_logins_never_pass_the_limit(db):
    add_user(db, "alice@example.com")
    svc = make_service(db, clock=lambda: datetime.now(UTC))
    counting = _CountingHasher()
    svc._hasher = counting

    def attempt(i: int) -> str:
        try:
            svc.login("alice@example.com", "wrong password here", ip=f"10.0.1.{i}")
        except TooManyAttempts:
            return "limited"
        except InvalidCredentials:
            return "wrong"
        return "ok"

    with ThreadPoolExecutor(max_workers=20) as pool:
        outcomes = list(pool.map(attempt, range(20)))
    assert counting.calls <= svc.settings.max_failures
    assert outcomes.count("wrong") <= svc.settings.max_failures
    assert "ok" not in outcomes


def test_as11_a_totp_code_is_accepted_once_even_in_parallel(db):
    clock = Clock()
    svc = make_service(db, clock=clock)
    add_user(db, "alice@example.com")
    _, secret, _ = enrol(svc, "alice@example.com", clock)
    clock.advance(minutes=5)
    infos = [
        svc.session(svc.login("alice@example.com", PASSWORD).session.token, unsafe=False)
        for _ in range(6)
    ]
    code = pyotp.TOTP(secret).at(clock())
    barrier = threading.Barrier(len(infos))

    def verify(info) -> bool:
        barrier.wait()
        try:
            svc.verify_mfa(info, code=code)
        except InvalidCredentials:
            return False
        return True

    with ThreadPoolExecutor(max_workers=len(infos)) as pool:
        results = list(pool.map(verify, infos))
    assert results.count(True) == 1


def test_as12_admin_password_reset_revokes_the_users_tokens(db):
    svc = make_service(db)
    uid = add_user(db, "alice@example.com")
    _, token = svc.create_token(session_principal(uid, Role.TRADER), name="t", scopes=["read"])
    admin = session_principal(DEFAULT_OWNER_ID, Role.ADMIN)
    svc.reset_password(admin, uid, "another long passphrase")
    with pytest.raises(NotAuthenticated):
        svc.principal_for_bearer(token)


def test_as12_admin_second_factor_reset_revokes_the_users_tokens(db):
    svc = make_service(db)
    uid = add_user(db, "alice@example.com")
    _, token = svc.create_token(session_principal(uid, Role.TRADER), name="t", scopes=["read"])
    svc.reset_mfa(session_principal(DEFAULT_OWNER_ID, Role.ADMIN), uid)
    with pytest.raises(NotAuthenticated):
        svc.principal_for_bearer(token)

"""AuthService edge cases: credentials of people who were disabled or
removed, refused requests that must not count as guesses, second-factor
secrets that cannot be opened, and the input checks on tokens, users and
the bootstrap admin."""

from __future__ import annotations

import pyotp
import pytest

from stonks.accounts import DEFAULT_OWNER_ID, Role, UserRepository
from stonks.app.errors import ConflictError, NotFoundError, ValidationError
from stonks.auth import AuthNotConfigured, InvalidCredentials, NotAuthenticated, PasswordHasher
from stonks.security import KeyRing, SecretBox, generate_key
from stonks.store.state import SqliteState
from tests.integration.auth.helpers import (
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


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def svc(db, clock):
    return make_service(db, clock=clock, legacy="legacy-token-123")


def _sql(db, query: str, params=()):
    with SqliteState(db) as state:
        return state.sql(query, list(params))


def _exec(db, query: str, params=()):
    with SqliteState(db) as state:
        state.execute(query, list(params))


def _attempts(db) -> int:
    return _sql(db, "SELECT COUNT(*) AS n FROM login_attempts")[0]["n"]


ADMIN = session_principal(DEFAULT_OWNER_ID, Role.ADMIN)


# ---- credentials of people who are gone -------------------------------------------


def test_a_session_ends_when_its_user_is_disabled_behind_its_back(svc, db, clock):
    uid = add_user(db, "alice@example.com")
    session, _, _ = enrol(svc, "alice@example.com", clock)
    # Disabled straight in the store (not through update_user, which revokes).
    _exec(db, "UPDATE users SET status = 'disabled' WHERE id = ?", [uid])
    with pytest.raises(NotAuthenticated):
        svc.principal_for_session(session.token, unsafe=False)


def test_an_api_token_stops_working_when_its_user_is_disabled(svc, db):
    uid = add_user(db, "alice@example.com")
    _, token = svc.create_token(session_principal(uid, Role.TRADER), name="t", scopes=["read"])
    _exec(db, "UPDATE users SET status = 'disabled' WHERE id = ?", [uid])
    with pytest.raises(NotAuthenticated):
        svc.principal_for_bearer(token)


def test_the_legacy_token_needs_an_active_bootstrap_admin(svc, db):
    _exec(db, "UPDATE users SET status = 'disabled' WHERE id = ?", [DEFAULT_OWNER_ID])
    with pytest.raises(AuthNotConfigured):
        svc.principal_for_bearer("legacy-token-123")


def test_logging_out_an_unknown_session_writes_no_audit_row(svc, db):
    svc.logout("no-such-session", actor="user:nobody")
    assert _sql(db, "SELECT * FROM audit_log WHERE action = 'auth.logout'") == []


# ---- passwords -------------------------------------------------------------------


def test_login_upgrades_a_hash_made_with_old_settings(db, clock):
    weak = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
    uid = add_user(db, "alice@example.com")
    old = _sql(db, "SELECT password_hash FROM users WHERE id = ?", [uid])[0]["password_hash"]
    svc = make_service(db, clock=clock)
    svc._hasher = PasswordHasher(time_cost=2, memory_cost=16, parallelism=1)
    assert svc._hasher.needs_rehash(old) and not weak.needs_rehash(old)
    svc.login("alice@example.com", PASSWORD)
    new = _sql(db, "SELECT password_hash FROM users WHERE id = ?", [uid])[0]["password_hash"]
    assert new != old and not svc._hasher.needs_rehash(new)
    assert svc.login("alice@example.com", PASSWORD).user.id == uid


def test_an_unreadable_stored_hash_needs_a_rehash_and_never_verifies():
    hasher = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
    assert hasher.needs_rehash("not-a-hash") is True
    assert hasher.verify("not-a-hash", PASSWORD) is False


# ---- second factor: refused requests are not guesses ----------------------------


def test_enrolment_is_refused_once_a_factor_is_set_up(svc, db, clock):
    add_user(db, "alice@example.com")
    enrol(svc, "alice@example.com", clock)
    login = svc.login("alice@example.com", PASSWORD)
    info = svc.session(login.session.token, unsafe=False)
    with pytest.raises(ConflictError):
        svc.enrol_start(info)
    before = _attempts(db)
    with pytest.raises(ConflictError):
        svc.enrol_confirm(info, "123456")
    assert _attempts(db) == before  # released, not counted


def test_confirming_before_starting_the_set_up_is_refused_and_not_counted(svc, db):
    add_user(db, "alice@example.com")
    info = svc.session(svc.login("alice@example.com", PASSWORD).session.token, unsafe=False)
    before = _attempts(db)
    with pytest.raises(ConflictError, match="start the set-up"):
        svc.enrol_confirm(info, "123456")
    assert _attempts(db) == before


def test_verify_needs_exactly_one_kind_of_code(svc, db, clock):
    add_user(db, "alice@example.com")
    session, _, codes = enrol(svc, "alice@example.com", clock)
    info = svc.session(session.token, unsafe=False)
    with pytest.raises(ValidationError):
        svc.verify_mfa(info, code="123456", recovery_code=codes[0])
    with pytest.raises(ValidationError):
        svc.verify_mfa(info)


def test_verify_before_enrolment_is_refused_and_not_counted(svc, db):
    add_user(db, "alice@example.com")
    info = svc.session(svc.login("alice@example.com", PASSWORD).session.token, unsafe=False)
    before = _attempts(db)
    with pytest.raises(ConflictError, match="enrol first"):
        svc.verify_mfa(info, code="123456")
    assert _attempts(db) == before


def test_a_factor_marked_enrolled_without_a_secret_asks_to_enrol(svc, db, clock):
    uid = add_user(db, "alice@example.com")
    enrol(svc, "alice@example.com", clock)
    _exec(db, "UPDATE users SET totp_secret_enc = NULL WHERE id = ?", [uid])
    info = svc.session(svc.login("alice@example.com", PASSWORD).session.token, unsafe=False)
    with pytest.raises(ConflictError, match="enrol first"):
        svc.verify_mfa(info, code="123456")


def test_a_secret_sealed_with_a_lost_key_says_so(db, clock):
    add_user(db, "alice@example.com")
    first = make_service(db, clock=clock)
    _, secret, _ = enrol(first, "alice@example.com", clock)
    other_key = SecretBox(KeyRing.parse(f"k9:{generate_key()}"))
    second = make_service(db, clock=clock, box=other_key)
    info = second.session(second.login("alice@example.com", PASSWORD).session.token, unsafe=False)
    clock.advance(seconds=60)
    with pytest.raises(AuthNotConfigured, match="STONKS_SECRET_KEYS"):
        second.verify_mfa(info, code=pyotp.TOTP(secret).at(clock()))


def test_a_code_already_claimed_by_a_parallel_check_is_refused(svc, db, clock):
    uid = add_user(db, "alice@example.com")
    _, secret, _ = enrol(svc, "alice@example.com", clock)
    clock.advance(seconds=60)
    info = svc.session(svc.login("alice@example.com", PASSWORD).session.token, unsafe=False)
    code = pyotp.TOTP(secret).at(clock())
    step = int(clock().timestamp()) // 30
    real_verify = svc._totp.verify

    def verify_then_lose_the_race(*args, **kwargs):
        found = real_verify(*args, **kwargs)
        # Another request claims this step between our read and our write.
        _exec(db, "UPDATE users SET totp_last_step = ? WHERE id = ?", [step, uid])
        return found

    svc._totp.verify = verify_then_lose_the_race
    with pytest.raises(InvalidCredentials):
        svc.verify_mfa(info, code=code)


def test_recovery_codes_need_a_factor_first(svc, db):
    uid = add_user(db, "alice@example.com")
    with pytest.raises(ConflictError):
        svc.regenerate_recovery_codes(session_principal(uid, Role.TRADER))


# ---- tokens and users --------------------------------------------------------------


def test_a_token_needs_a_scope_and_a_name(svc, db):
    uid = add_user(db, "alice@example.com")
    me = session_principal(uid, Role.TRADER)
    with pytest.raises(ValidationError, match="scope"):
        svc.create_token(me, name="t", scopes=[])
    with pytest.raises(ValidationError, match="name"):
        svc.create_token(me, name="   ", scopes=["read"])


def test_revoking_a_token_twice_audits_once(svc, db):
    uid = add_user(db, "alice@example.com")
    me = session_principal(uid, Role.TRADER)
    info, _ = svc.create_token(me, name="t", scopes=["read"])
    svc.revoke_token(me, info.id)
    svc.revoke_token(me, info.id)
    rows = _sql(db, "SELECT * FROM audit_log WHERE action = 'auth.token.revoke'")
    assert len(rows) == 1


def test_a_new_user_needs_a_real_email(svc):
    with pytest.raises(ValidationError, match="email"):
        svc.create_user(
            ADMIN, email="not-an-email", display_name="X", role=Role.TRADER, password=PASSWORD
        )


def test_an_admin_can_be_demoted_while_another_admin_remains(svc, db):
    other = add_user(db, "second@example.com", role=Role.ADMIN)
    updated = svc.update_user(ADMIN, other, role=Role.TRADER)
    assert updated.user.role is Role.TRADER


def test_service_users_are_not_managed_as_people(svc, db):
    with SqliteState(db) as state:
        bot = UserRepository(state).create(
            display_name="bot", role=Role.VIEWER, actor="service:test", kind="service"
        )
    with pytest.raises(NotFoundError):
        svc.update_user(ADMIN, bot.id, role=Role.TRADER)
    with pytest.raises(NotFoundError):
        svc.reset_mfa(ADMIN, bot.id)


def test_is_active_user_follows_the_status(svc, db):
    uid = add_user(db, "alice@example.com")
    assert svc.is_active_user(uid) is True
    _exec(db, "UPDATE users SET status = 'disabled' WHERE id = ?", [uid])
    assert svc.is_active_user(uid) is False
    assert svc.is_active_user("usr_missing") is False


# ---- bootstrap -----------------------------------------------------------------------


def test_bootstrap_needs_a_real_email_that_nobody_else_has(svc, db):
    with pytest.raises(ValidationError, match="email"):
        svc.bootstrap_admin("owner", PASSWORD)
    add_user(db, "taken@example.com")
    with pytest.raises(ConflictError, match="another user"):
        svc.bootstrap_admin("taken@example.com", PASSWORD)


def test_resetting_an_unknown_email_is_not_found(svc):
    with pytest.raises(NotFoundError):
        svc.set_password_by_email("nobody@example.com", "a long enough passphrase")

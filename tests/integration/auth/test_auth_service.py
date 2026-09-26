"""AuthService over a real state file: login, mandatory second factor,
sessions, rate limit, API tokens, user administration, bootstrap."""

from __future__ import annotations

import json
from datetime import timedelta

import pyotp
import pytest

from stonks.accounts import DEFAULT_OWNER_ID, Role
from stonks.app.errors import ConflictError, NotFoundError, ValidationError
from stonks.auth import (
    ApiScope,
    AuthNotConfigured,
    CsrfFailed,
    InvalidCredentials,
    MfaRequired,
    NotAuthenticated,
    PermissionDenied,
    StepUpRequired,
    TooManyAttempts,
)
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


def _audit(db, action: str) -> list:
    with SqliteState(db) as state:
        return state.sql("SELECT * FROM audit_log WHERE action = ? ORDER BY id", [action])


# ---- migration ---------------------------------------------------------------


def test_migration_adds_auth_tables(db):
    with SqliteState(db) as state:
        assert {"sessions", "api_tokens", "recovery_codes", "login_attempts"} <= set(state.tables())
        cols = {r["name"] for r in state.sql("PRAGMA table_info(users)")}
        assert {"mfa_enrolled_at", "totp_last_step", "password_changed_at"} <= cols


# ---- login and second factor -------------------------------------------------


def test_first_login_must_enrol_totp_before_any_access(svc, db, clock):
    uid = add_user(db, "alice@example.com")
    login = svc.login("Alice@Example.com", PASSWORD, ip="10.0.0.1")
    assert login.next_step == "enrol" and login.user.id == uid
    with pytest.raises(MfaRequired):
        svc.principal_for_session(login.session.token, unsafe=False)

    info = svc.session(login.session.token, unsafe=False)
    start = svc.enrol_start(info)
    assert start.otpauth_uri.startswith("otpauth://totp/")
    with SqliteState(db) as state:
        (row,) = state.sql("SELECT totp_secret_enc FROM users WHERE id = ?", [uid])
    assert start.secret not in row["totp_secret_enc"]  # sealed at rest

    with pytest.raises(InvalidCredentials):
        svc.enrol_confirm(info, "000000")
    result = svc.enrol_confirm(info, pyotp.TOTP(start.secret).at(clock()))
    assert len(result.recovery_codes) == 10
    # The pending session is replaced; the old cookie value is dead.
    with pytest.raises(NotAuthenticated):
        svc.session(login.session.token, unsafe=False)
    principal = svc.principal_for_session(result.session.token, unsafe=False)
    assert principal.user_id == uid and principal.via == "session" and principal.mfa_fresh
    assert _audit(db, "auth.mfa.enrol")[0]["target_id"] == uid
    assert _audit(db, "auth.login.password")


def test_later_login_verifies_totp_and_rejects_replay(svc, db, clock):
    add_user(db, "alice@example.com")
    _, secret, _ = enrol(svc, "alice@example.com", clock)
    clock.advance(minutes=5)
    login = svc.login("alice@example.com", PASSWORD)
    assert login.next_step == "verify"
    info = svc.session(login.session.token, unsafe=False)
    code = pyotp.TOTP(secret).at(clock())
    result = svc.verify_mfa(info, code=code)
    assert result.session is not None and result.method == "totp"

    again = svc.session(svc.login("alice@example.com", PASSWORD).session.token, unsafe=False)
    with pytest.raises(InvalidCredentials):
        svc.verify_mfa(again, code=code)  # same step: replay


def test_recovery_code_works_once(svc, db, clock):
    add_user(db, "alice@example.com")
    _, _, codes = enrol(svc, "alice@example.com", clock)
    info = svc.session(svc.login("alice@example.com", PASSWORD).session.token, unsafe=False)
    result = svc.verify_mfa(info, recovery_code=codes[0].upper())
    assert result.method == "recovery_code" and result.recovery_codes_left == 9
    info2 = svc.session(svc.login("alice@example.com", PASSWORD).session.token, unsafe=False)
    with pytest.raises(InvalidCredentials):
        svc.verify_mfa(info2, recovery_code=codes[0])


def test_wrong_password_and_unknown_email_read_the_same(svc, db):
    add_user(db, "alice@example.com")
    with pytest.raises(InvalidCredentials) as wrong:
        svc.login("alice@example.com", "nope nope nope nope")
    with pytest.raises(InvalidCredentials) as unknown:
        svc.login("nobody@example.com", "nope nope nope nope")
    assert str(wrong.value) == str(unknown.value)
    assert len(_audit(db, "auth.login_failed")) == 1  # only known accounts are audited


def test_disabled_user_cannot_log_in(svc, db):
    uid = add_user(db, "alice@example.com")
    with SqliteState(db) as state:
        state.execute("UPDATE users SET status = 'disabled' WHERE id = ?", [uid])
    with pytest.raises(InvalidCredentials):
        svc.login("alice@example.com", PASSWORD)


def test_login_rate_limit_per_account_then_window_expires(svc, db, clock):
    add_user(db, "alice@example.com")
    for i in range(5):
        with pytest.raises(InvalidCredentials):
            svc.login("alice@example.com", "wrong password here", ip=f"10.0.0.{i}")
    with pytest.raises(TooManyAttempts) as exc:
        svc.login("alice@example.com", PASSWORD, ip="10.0.0.99")  # even the right one
    assert int(exc.value.headers["Retry-After"]) > 0
    clock.advance(minutes=16)
    assert svc.login("alice@example.com", PASSWORD, ip="10.0.0.99").next_step == "enrol"


def test_login_rate_limit_per_ip(svc, db):
    for i in range(5):
        with pytest.raises(InvalidCredentials):
            svc.login(f"user{i}@example.com", "wrong password here", ip="10.9.9.9")
    add_user(db, "alice@example.com")
    with pytest.raises(TooManyAttempts):
        svc.login("alice@example.com", PASSWORD, ip="10.9.9.9")
    assert svc.login("alice@example.com", PASSWORD, ip="10.1.1.1")


def test_second_factor_failures_count_toward_the_limit(svc, db, clock):
    add_user(db, "alice@example.com")
    enrol(svc, "alice@example.com", clock)
    info = svc.session(svc.login("alice@example.com", PASSWORD).session.token, unsafe=False)
    for _ in range(5):
        with pytest.raises(InvalidCredentials):
            svc.verify_mfa(info, code="000000")
    with pytest.raises(TooManyAttempts):
        svc.verify_mfa(info, code="000000")


# ---- sessions ----------------------------------------------------------------


def test_session_needs_csrf_for_unsafe_methods(svc, db, clock):
    add_user(db, "alice@example.com")
    session, _, _ = enrol(svc, "alice@example.com", clock)
    with pytest.raises(CsrfFailed):
        svc.principal_for_session(session.token, unsafe=True)
    with pytest.raises(CsrfFailed):
        svc.principal_for_session(session.token, csrf="forged", unsafe=True)
    assert svc.principal_for_session(session.token, csrf=session.csrf_token, unsafe=True)


def test_session_idle_and_absolute_timeouts(svc, db, clock):
    add_user(db, "alice@example.com")
    session, _, _ = enrol(svc, "alice@example.com", clock)
    clock.advance(hours=11)
    assert svc.principal_for_session(session.token, unsafe=False)
    clock.advance(hours=11)  # touched 11 h ago: still inside the idle window
    assert svc.principal_for_session(session.token, unsafe=False)
    clock.advance(hours=13)
    with pytest.raises(NotAuthenticated):
        svc.principal_for_session(session.token, unsafe=False)


def test_session_absolute_timeout_even_when_active(svc, db, clock):
    add_user(db, "alice@example.com")
    session, _, _ = enrol(svc, "alice@example.com", clock)
    while clock() + timedelta(hours=11) < session.expires_at:
        clock.advance(hours=11)
        assert svc.principal_for_session(session.token, unsafe=False)
    clock.now = session.expires_at
    with pytest.raises(NotAuthenticated):
        svc.principal_for_session(session.token, unsafe=False)


def test_pending_session_expires_quickly(svc, db, clock):
    add_user(db, "alice@example.com")
    login = svc.login("alice@example.com", PASSWORD)
    clock.advance(minutes=11)
    with pytest.raises(NotAuthenticated):
        svc.session(login.session.token, unsafe=False)


def test_step_up_freshness_and_refresh(svc, db, clock):
    add_user(db, "alice@example.com")
    session, secret, _ = enrol(svc, "alice@example.com", clock)
    clock.advance(minutes=11)
    stale = svc.principal_for_session(session.token, unsafe=False)
    assert not stale.mfa_fresh
    info = svc.session(session.token, unsafe=False)
    result = svc.verify_mfa(info, code=pyotp.TOTP(secret).at(clock()))
    assert result.session is None  # same session, now fresh again
    assert svc.principal_for_session(session.token, unsafe=False).mfa_fresh


def test_logout_revokes_the_session(svc, db, clock):
    add_user(db, "alice@example.com")
    session, _, _ = enrol(svc, "alice@example.com", clock)
    p = svc.principal_for_session(session.token, unsafe=False)
    svc.logout(p.credential_id, actor=p.actor)
    with pytest.raises(NotAuthenticated):
        svc.principal_for_session(session.token, unsafe=False)
    assert _audit(db, "auth.logout")


def test_change_password_signs_out_other_sessions(svc, db, clock):
    add_user(db, "alice@example.com")
    first, secret, _ = enrol(svc, "alice@example.com", clock)
    clock.advance(seconds=31)
    login = svc.login("alice@example.com", PASSWORD)
    second = svc.verify_mfa(
        svc.session(login.session.token, unsafe=False), code=pyotp.TOTP(secret).at(clock())
    ).session
    p = svc.principal_for_session(second.token, unsafe=False)
    with pytest.raises(InvalidCredentials):
        svc.change_password(p, "wrong", "a brand new passphrase")
    with pytest.raises(ValidationError):
        svc.change_password(p, PASSWORD, "short")
    svc.change_password(p, PASSWORD, "a brand new passphrase")
    with pytest.raises(NotAuthenticated):
        svc.principal_for_session(first.token, unsafe=False)
    assert svc.principal_for_session(second.token, unsafe=False)
    assert svc.login("alice@example.com", "a brand new passphrase")


# ---- API tokens --------------------------------------------------------------


def test_token_lifecycle(svc, db):
    uid = add_user(db, "alice@example.com")
    p = session_principal(uid, Role.TRADER)
    info, token = svc.create_token(p, name="mcp", scopes=["read", "lab"], expires_in_days=30)
    assert token.startswith(f"stk_{info.id}_")
    with SqliteState(db) as state:
        (row,) = state.sql("SELECT token_hash, scopes FROM api_tokens")
    assert token not in row["token_hash"] and json.loads(row["scopes"]) == ["read", "lab"]

    tp = svc.principal_for_bearer(token)
    assert tp.user_id == uid and tp.via == "token"
    assert tp.scopes == frozenset({ApiScope.READ, ApiScope.LAB})
    assert [t.id for t in svc.list_tokens(p)] == [info.id]

    svc.revoke_token(p, info.id)
    with pytest.raises(NotAuthenticated):
        svc.principal_for_bearer(token)
    assert _audit(db, "auth.token.create") and _audit(db, "auth.token.revoke")


def test_token_rules(svc, db, clock):
    uid = add_user(db, "viewer@example.com", Role.VIEWER)
    viewer = session_principal(uid, Role.VIEWER)
    with pytest.raises(PermissionDenied):
        svc.create_token(viewer, name="x", scopes=["trade"])
    tid = add_user(db, "trader@example.com")
    stale = session_principal(tid, Role.TRADER, mfa_fresh=False)
    with pytest.raises(StepUpRequired):
        svc.create_token(stale, name="x", scopes=["trade"])
    assert svc.create_token(stale, name="ro", scopes=["read"])
    _, token = svc.create_token(session_principal(tid, Role.TRADER), name="t", scopes=["trade"])
    # A token can't mint tokens.
    with pytest.raises(PermissionDenied):
        svc.create_token(svc.principal_for_bearer(token), name="y", scopes=["read"])
    # Expiry.
    _, short = svc.create_token(stale, name="short", scopes=["read"], expires_in_days=1)
    clock.advance(days=2)
    with pytest.raises(NotAuthenticated):
        svc.principal_for_bearer(short)


def test_token_scopes_shrink_when_the_role_is_lowered(svc, db):
    uid = add_user(db, "alice@example.com")
    _, token = svc.create_token(
        session_principal(uid, Role.TRADER), name="t", scopes=["read", "trade"]
    )
    with SqliteState(db) as state:
        state.execute("UPDATE users SET role = 'viewer' WHERE id = ?", [uid])
    assert svc.principal_for_bearer(token).scopes == frozenset({ApiScope.READ})


def test_other_users_token_reads_as_not_found(svc, db):
    a = add_user(db, "alice@example.com")
    b = add_user(db, "bob@example.com")
    info, _ = svc.create_token(session_principal(b, Role.TRADER), name="b", scopes=["read"])
    alice = session_principal(a, Role.TRADER)
    with pytest.raises(NotFoundError):
        svc.revoke_token(alice, info.id)
    assert svc.list_tokens(alice) == []


def test_forged_or_malformed_api_token(svc, db):
    uid = add_user(db, "alice@example.com")
    info, token = svc.create_token(session_principal(uid, Role.TRADER), name="t", scopes=["read"])
    forged = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")
    for bad in (forged, "stk_short", f"stk_{info.id}_"):
        with pytest.raises(NotAuthenticated):
            svc.principal_for_bearer(bad)


# ---- legacy shim -------------------------------------------------------------


def test_legacy_env_token_maps_to_bootstrap_admin(svc, db):
    p = svc.principal_for_bearer("legacy-token-123")
    assert p.user_id == DEFAULT_OWNER_ID and p.via == "legacy" and p.role is Role.ADMIN
    with pytest.raises(NotAuthenticated):
        svc.principal_for_bearer("legacy-token-12")


def test_legacy_token_not_configured(db):
    svc = make_service(db, legacy=None)
    with pytest.raises(AuthNotConfigured):
        svc.principal_for_bearer("anything")


# ---- user administration -----------------------------------------------------


def test_admin_manages_users_with_step_up(svc, db):
    admin = session_principal(DEFAULT_OWNER_ID, Role.ADMIN)
    created = svc.create_user(
        admin, email="carol@example.com", display_name="Carol", role=Role.TRADER, password=PASSWORD
    )
    assert svc.login("carol@example.com", PASSWORD).next_step == "enrol"
    with pytest.raises(ConflictError):
        svc.create_user(
            admin, email="CAROL@example.com", display_name="C", role=Role.VIEWER, password=PASSWORD
        )
    with pytest.raises(StepUpRequired):
        svc.create_user(
            session_principal(DEFAULT_OWNER_ID, Role.ADMIN, mfa_fresh=False),
            email="dave@example.com",
            display_name="Dave",
            role=Role.VIEWER,
            password=PASSWORD,
        )
    trader = session_principal(created.user.id, Role.TRADER)
    with pytest.raises(PermissionDenied):
        svc.list_users(trader)

    listed = {u.user.email: u for u in svc.list_users(admin)}
    assert "carol@example.com" in listed

    updated = svc.update_user(admin, created.user.id, role=Role.VIEWER)
    assert updated.user.role is Role.VIEWER
    assert _audit(db, "user.role")


def test_disabling_a_user_revokes_sessions_and_tokens(svc, db, clock):
    uid = add_user(db, "alice@example.com")
    session, _, _ = enrol(svc, "alice@example.com", clock)
    _, token = svc.create_token(session_principal(uid, Role.TRADER), name="t", scopes=["read"])
    admin = session_principal(DEFAULT_OWNER_ID, Role.ADMIN)
    svc.update_user(admin, uid, status="disabled")
    with pytest.raises(NotAuthenticated):
        svc.principal_for_session(session.token, unsafe=False)
    with pytest.raises(NotAuthenticated):
        svc.principal_for_bearer(token)
    # Re-enabling does not bring the old credentials back.
    svc.update_user(admin, uid, status="active")
    with pytest.raises(NotAuthenticated):
        svc.principal_for_bearer(token)


def test_last_admin_cannot_be_demoted_or_disabled(svc):
    admin = session_principal(DEFAULT_OWNER_ID, Role.ADMIN)
    with pytest.raises(ConflictError):
        svc.update_user(admin, DEFAULT_OWNER_ID, role=Role.TRADER)
    with pytest.raises(ConflictError):
        svc.update_user(admin, DEFAULT_OWNER_ID, status="disabled")


def test_admin_resets_password_and_second_factor(svc, db, clock):
    uid = add_user(db, "alice@example.com")
    session, _, _ = enrol(svc, "alice@example.com", clock)
    admin = session_principal(DEFAULT_OWNER_ID, Role.ADMIN)
    svc.reset_mfa(admin, uid)
    with pytest.raises(NotAuthenticated):
        svc.principal_for_session(session.token, unsafe=False)
    assert svc.login("alice@example.com", PASSWORD).next_step == "enrol"
    svc.reset_password(admin, uid, "another long passphrase")
    assert svc.login("alice@example.com", "another long passphrase")
    with pytest.raises(NotFoundError):
        svc.reset_password(admin, "usr_missing", "another long passphrase")


def test_regenerate_recovery_codes_needs_step_up(svc, db, clock):
    uid = add_user(db, "alice@example.com")
    enrol(svc, "alice@example.com", clock)
    with pytest.raises(StepUpRequired):
        svc.regenerate_recovery_codes(session_principal(uid, Role.TRADER, mfa_fresh=False))
    codes = svc.regenerate_recovery_codes(session_principal(uid, Role.TRADER))
    assert len(codes) == 10


# ---- bootstrap -----------------------------------------------------------------


def test_bootstrap_admin_then_first_login(svc, db):
    user = svc.bootstrap_admin("owner@example.com", PASSWORD)
    assert user.id == DEFAULT_OWNER_ID and user.email == "owner@example.com"
    assert svc.login("owner@example.com", PASSWORD).next_step == "enrol"
    with pytest.raises(ConflictError):
        svc.bootstrap_admin("owner@example.com", PASSWORD)
    svc.set_password_by_email("owner@example.com", "a different passphrase")
    assert svc.login("owner@example.com", "a different passphrase")
    assert _audit(db, "auth.bootstrap")


def test_enrolment_needs_the_encryption_key(db, monkeypatch):
    from contextlib import contextmanager

    from stonks.auth import AuthService
    from tests.integration.auth.helpers import FAST_HASHER

    monkeypatch.delenv("STONKS_SECRET_KEYS", raising=False)
    monkeypatch.delenv("STONKS_SECRET_KEY_FILE", raising=False)

    @contextmanager
    def factory():
        with SqliteState(db) as state:
            yield state

    svc = AuthService(factory, hasher=FAST_HASHER)
    add_user(db, "alice@example.com")
    info = svc.session(svc.login("alice@example.com", PASSWORD).session.token, unsafe=False)
    with pytest.raises(AuthNotConfigured):
        svc.enrol_start(info)

"""Auth building blocks: password hashing, TOTP, recovery codes, token
format, principals and the policy table. Pure, no database."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pyotp
import pytest

from stonks.accounts import Role, Scope
from stonks.auth.credentials import hash_secret, mint_api_token, parse_api_token
from stonks.auth.errors import PermissionDenied, StepUpRequired
from stonks.auth.passwords import PasswordHasher, PasswordPolicyError
from stonks.auth.policy import Permission, allowed, require
from stonks.auth.principal import ROLE_SCOPES, ApiScope, Principal
from stonks.auth.recovery import generate_codes, hash_code
from stonks.auth.totp import Totp

FAST = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)


# ---- passwords -------------------------------------------------------------


def test_password_round_trip_uses_argon2id():
    stored = FAST.hash("correct horse battery")
    assert stored.startswith("$argon2id$")
    assert FAST.verify(stored, "correct horse battery")
    assert not FAST.verify(stored, "wrong horse battery")


def test_password_verify_is_false_for_missing_or_garbage_hash():
    assert not FAST.verify(None, "correct horse battery")
    assert not FAST.verify("not-a-hash", "correct horse battery")


@pytest.mark.parametrize("bad", ["short", "x" * 11, "x" * 257, "   " * 5])
def test_password_policy(bad):
    with pytest.raises(PasswordPolicyError):
        FAST.hash(bad)


def test_needs_rehash_when_parameters_change():
    stored = FAST.hash("correct horse battery")
    stronger = PasswordHasher(time_cost=2, memory_cost=8, parallelism=1)
    assert stronger.needs_rehash(stored)
    assert not FAST.needs_rehash(stored)


# ---- TOTP ------------------------------------------------------------------

NOW = datetime(2026, 9, 26, 12, 0, 0, tzinfo=UTC)


def test_totp_accepts_current_code_and_returns_its_step():
    totp = Totp()
    secret = totp.new_secret()
    code = pyotp.TOTP(secret).at(NOW)
    step = totp.verify(secret, code, last_step=None, at=NOW)
    assert step == pyotp.TOTP(secret).timecode(NOW)


def test_totp_allows_one_step_of_clock_drift_only():
    totp = Totp()
    secret = totp.new_secret()
    prev = pyotp.TOTP(secret).at(NOW - timedelta(seconds=30))
    old = pyotp.TOTP(secret).at(NOW - timedelta(seconds=90))
    assert totp.verify(secret, prev, last_step=None, at=NOW) is not None
    assert totp.verify(secret, old, last_step=None, at=NOW) is None


def test_totp_rejects_replay_of_a_used_step():
    totp = Totp()
    secret = totp.new_secret()
    code = pyotp.TOTP(secret).at(NOW)
    step = totp.verify(secret, code, last_step=None, at=NOW)
    assert totp.verify(secret, code, last_step=step, at=NOW) is None


@pytest.mark.parametrize("junk", ["", "12345", "abcdef", "1234567"])
def test_totp_rejects_malformed_codes(junk):
    totp = Totp()
    assert totp.verify(totp.new_secret(), junk, last_step=None, at=NOW) is None


def test_totp_provisioning_uri_names_issuer_and_account():
    totp = Totp(issuer="Stonks")
    uri = totp.provisioning_uri("JBSWY3DPEHPK3PXP", "alice@example.com")
    assert uri.startswith("otpauth://totp/")
    assert "issuer=Stonks" in uri and "alice%40example.com" in uri


# ---- recovery codes and token format -----------------------------------------


def test_recovery_codes_are_ten_distinct_80_bit_codes():
    codes = generate_codes()
    assert len(codes) == 10 and len(set(codes)) == 10
    assert all(len(c.replace("-", "")) == 16 for c in codes)


def test_recovery_code_hash_ignores_case_dashes_and_spaces():
    code = generate_codes(1)[0]
    assert hash_code(code) == hash_code(" " + code.upper().replace("-", " ") + " ")
    assert hash_code(code) != hash_code(generate_codes(1)[0])


def test_api_token_format_and_parse():
    token_id, token = mint_api_token()
    assert token.startswith(f"stk_{token_id}_")
    assert parse_api_token(token) == token_id
    assert parse_api_token("stk_nope") is None
    assert parse_api_token("legacy-token") is None
    assert hash_secret(token) != token and len(hash_secret(token)) == 64


# ---- principal and policy ----------------------------------------------------


def _principal(role: Role, scopes=None, *, via="session", mfa_fresh=False) -> Principal:
    return Principal(
        user_id="usr_a",
        kind="human",
        role=role,
        scopes=frozenset(ROLE_SCOPES[role] if scopes is None else scopes),
        mfa_fresh=mfa_fresh,
        via=via,
    )


def test_role_scopes_never_exceed_the_role():
    assert ROLE_SCOPES[Role.VIEWER] == {ApiScope.READ}
    assert ROLE_SCOPES[Role.TRADER] == {ApiScope.READ, ApiScope.TRADE, ApiScope.LAB}
    assert ROLE_SCOPES[Role.ADMIN] == set(ApiScope)


def test_principal_actor_and_data_scope():
    p = _principal(Role.TRADER)
    assert p.actor == "user:usr_a"
    assert p.scope == Scope(user_id="usr_a", role=Role.TRADER)
    svc = Principal.service("scheduler")
    assert svc.actor == "service:scheduler" and svc.scope.is_service


def test_principal_clamps_scopes_to_role():
    p = Principal.create(
        user_id="usr_a",
        kind="human",
        role=Role.VIEWER,
        scopes={ApiScope.READ, ApiScope.ADMIN},
        mfa_fresh=False,
        via="token",
    )
    assert p.scopes == frozenset({ApiScope.READ})


def test_policy_by_role():
    assert allowed(_principal(Role.VIEWER), Permission.READ)
    assert not allowed(_principal(Role.VIEWER), Permission.PORTFOLIO_TRADE)
    assert allowed(_principal(Role.TRADER), Permission.PORTFOLIO_TRADE)
    assert not allowed(_principal(Role.TRADER), Permission.USERS_MANAGE)
    with pytest.raises(PermissionDenied):
        require(_principal(Role.TRADER), Permission.USERS_MANAGE)


def test_policy_by_token_scope():
    read_only = _principal(Role.ADMIN, {ApiScope.READ}, via="token")
    assert allowed(read_only, Permission.READ)
    assert not allowed(read_only, Permission.LAB_RUN)
    assert not allowed(read_only, Permission.USERS_MANAGE)


def test_step_up_needs_a_fresh_second_factor_on_a_session():
    stale = _principal(Role.ADMIN)
    with pytest.raises(StepUpRequired):
        require(stale, Permission.USERS_MANAGE)
    require(_principal(Role.ADMIN, mfa_fresh=True), Permission.USERS_MANAGE)


def test_tokens_can_never_do_step_up_or_session_only_actions():
    token = _principal(Role.ADMIN, via="token", mfa_fresh=True)
    with pytest.raises(StepUpRequired):
        require(token, Permission.USERS_MANAGE)
    with pytest.raises(PermissionDenied):
        require(token, Permission.TOKENS_MANAGE)


def test_the_lab_worker_scope_is_admin_only_and_confined():
    """A remote lab worker's token (roadmap 14.9): only admins hold the
    scope, it grants only the worker permission, and a token holding only
    it is confined to the worker routes."""
    assert ApiScope.LAB_WORKER not in ROLE_SCOPES[Role.TRADER]
    worker = _principal(Role.ADMIN, {ApiScope.LAB_WORKER}, via="token")
    assert worker.confined and worker.can_write
    assert allowed(worker, Permission.LAB_WORKER)
    assert not allowed(worker, Permission.READ)
    assert not allowed(worker, Permission.LAB_RUN)
    assert not allowed(_principal(Role.TRADER, via="token"), Permission.LAB_WORKER)
    assert not allowed(_principal(Role.ADMIN, {ApiScope.LAB}, via="token"), Permission.LAB_WORKER)
    mixed = _principal(Role.ADMIN, {ApiScope.LAB_WORKER, ApiScope.READ}, via="token")
    assert not mixed.confined
    assert not _principal(Role.ADMIN, set(), via="token").confined


def test_every_permission_has_a_rule():
    from stonks.auth.policy import POLICY

    assert set(POLICY) == set(Permission)

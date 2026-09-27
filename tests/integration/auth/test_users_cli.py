"""``stonks users bootstrap|reset-password|list``: operator commands over
AuthService. Shell access implies admin; the password never comes from argv."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from stonks.accounts import DEFAULT_OWNER_ID, UserRepository
from stonks.cli import app
from stonks.store.state import SqliteState
from tests.integration.auth.helpers import FAST_HASHER, PASSWORD


@pytest.fixture
def state_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("STONKS_DATA_DIR", str(tmp_path / "data"))
    (tmp_path / "data").mkdir()
    path = tmp_path / "data" / "state.sqlite"
    with SqliteState(path) as state:
        state.migrate()
    monkeypatch.setattr("stonks.auth.service.PasswordHasher", lambda: FAST_HASHER)
    return path


def _run(*args: str):
    return CliRunner().invoke(app, ["users", *args])


def test_bootstrap_then_list_then_reset(state_path, monkeypatch):
    monkeypatch.setenv("STONKS_AUTH_PASSWORD", PASSWORD)
    result = _run("bootstrap", "--email", "Owner@Example.com")
    assert result.exit_code == 0, result.output
    assert "owner@example.com" in result.output
    with SqliteState(state_path) as state:
        owner = UserRepository(state).get(DEFAULT_OWNER_ID)
        (row,) = state.sql("SELECT password_hash FROM users WHERE id = ?", [DEFAULT_OWNER_ID])
    assert owner.email == "owner@example.com"
    assert FAST_HASHER.verify(row["password_hash"], PASSWORD)

    again = _run("bootstrap", "--email", "owner@example.com")
    assert again.exit_code == 1 and "already has a password" in again.output

    listed = _run("list")
    assert listed.exit_code == 0, listed.output
    assert "owner@example.com" in listed.output and "admin" in listed.output

    monkeypatch.setenv("STONKS_AUTH_PASSWORD", "a different passphrase")
    reset = _run("reset-password", "--email", "OWNER@example.com")
    assert reset.exit_code == 0, reset.output
    with SqliteState(state_path) as state:
        (row,) = state.sql("SELECT password_hash FROM users WHERE id = ?", [DEFAULT_OWNER_ID])
    assert FAST_HASHER.verify(row["password_hash"], "a different passphrase")
    assert _run("reset-password", "--email", "nobody@example.com").exit_code == 1


def test_password_is_prompted_without_echo_when_env_is_unset(state_path, monkeypatch):
    monkeypatch.delenv("STONKS_AUTH_PASSWORD", raising=False)
    answers = iter([PASSWORD, PASSWORD])
    monkeypatch.setattr("getpass.getpass", lambda prompt="": next(answers))
    result = _run("bootstrap", "--email", "owner@example.com")
    assert result.exit_code == 0, result.output
    assert PASSWORD not in result.output


def test_there_is_no_password_option(state_path):
    result = _run("bootstrap", "--email", "owner@example.com", "--password", PASSWORD)
    assert result.exit_code != 0


def _user(state_path, email: str):
    with SqliteState(state_path) as state:
        return UserRepository(state).get_by_email(email)


def test_create_a_trader_then_change_role_and_status(state_path, monkeypatch):
    monkeypatch.setenv("STONKS_AUTH_PASSWORD", PASSWORD)
    made = _run("create", "--email", "Tess@Example.com", "--name", "Tess", "--role", "trader")
    assert made.exit_code == 0, made.output
    tess = _user(state_path, "tess@example.com")
    assert (tess.display_name, tess.role.value, tess.status) == ("Tess", "trader", "active")
    assert _run("create", "--email", "tess@example.com", "--name", "T").exit_code == 1

    assert _run("set-role", "--email", "tess@example.com", "--role", "viewer").exit_code == 0
    assert _user(state_path, "tess@example.com").role.value == "viewer"
    assert _run("set-role", "--email", "tess@example.com", "--role", "boss").exit_code != 0

    assert _run("disable", "--email", "tess@example.com").exit_code == 0
    assert _user(state_path, "tess@example.com").status == "disabled"
    assert _run("enable", "--email", "tess@example.com").exit_code == 0
    assert _user(state_path, "tess@example.com").status == "active"
    with SqliteState(state_path) as state:
        actions = [r["action"] for r in state.sql("SELECT action FROM audit_log ORDER BY id")]
    assert "user.role" in actions


def test_reset_2fa_lets_a_locked_out_sole_admin_enrol_again(state_path, monkeypatch):
    monkeypatch.setenv("STONKS_AUTH_PASSWORD", PASSWORD)
    assert _run("bootstrap", "--email", "owner@example.com").exit_code == 0
    with SqliteState(state_path) as state:
        state.execute(
            "UPDATE users SET totp_secret_enc = 'x', mfa_enrolled_at = '2026-01-01' WHERE id = ?",
            [DEFAULT_OWNER_ID],
        )
        state.execute(
            "INSERT INTO recovery_codes (user_id, code_hash, created_at) VALUES (?, 'h', 'x')",
            [DEFAULT_OWNER_ID],
        )
    result = _run("reset-2fa", "--email", "owner@example.com")
    assert result.exit_code == 0, result.output
    with SqliteState(state_path) as state:
        (row,) = state.sql(
            "SELECT totp_secret_enc, mfa_enrolled_at FROM users WHERE id = ?", [DEFAULT_OWNER_ID]
        )
        codes = state.sql("SELECT COUNT(*) AS n FROM recovery_codes")[0]["n"]
        actions = [r["action"] for r in state.sql("SELECT action FROM audit_log ORDER BY id")]
    assert (row["totp_secret_enc"], row["mfa_enrolled_at"], codes) == (None, None, 0)
    assert actions[-1] == "auth.mfa.reset"
    assert _run("reset-2fa", "--email", "nobody@example.com").exit_code == 1


def test_the_last_admin_cannot_be_disabled_from_the_shell(state_path, monkeypatch):
    monkeypatch.setenv("STONKS_AUTH_PASSWORD", PASSWORD)
    assert _run("bootstrap", "--email", "owner@example.com").exit_code == 0
    result = _run("disable", "--email", "owner@example.com")
    assert result.exit_code == 1 and "last active admin" in result.output

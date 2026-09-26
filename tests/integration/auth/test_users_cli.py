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

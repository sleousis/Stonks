"""``python -m stonks.auth``: bootstrap the admin, reset a password."""

from __future__ import annotations

import pytest

from stonks.accounts import DEFAULT_OWNER_ID, UserRepository
from stonks.auth.__main__ import main
from stonks.store.state import SqliteState
from tests.integration.auth.helpers import FAST_HASHER, PASSWORD


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "state.sqlite"
    with SqliteState(path) as state:
        state.migrate()
    monkeypatch.setattr("stonks.auth.__main__._hasher", lambda: FAST_HASHER)
    return path


def test_bootstrap_admin_reads_password_from_env(db, monkeypatch, capsys):
    monkeypatch.setenv("STONKS_AUTH_PASSWORD", PASSWORD)
    rc = main(["bootstrap-admin", "--email", "owner@example.com", "--state", str(db)])
    assert rc == 0
    assert "owner@example.com" in capsys.readouterr().out
    with SqliteState(db) as state:
        user = UserRepository(state).get(DEFAULT_OWNER_ID)
        (row,) = state.sql("SELECT password_hash FROM users WHERE id = ?", [DEFAULT_OWNER_ID])
    assert user.email == "owner@example.com"
    assert FAST_HASHER.verify(row["password_hash"], PASSWORD)
    # Twice is refused.
    assert main(["bootstrap-admin", "--email", "owner@example.com", "--state", str(db)]) == 1


def test_reset_password(db, monkeypatch):
    monkeypatch.setenv("STONKS_AUTH_PASSWORD", PASSWORD)
    main(["bootstrap-admin", "--email", "owner@example.com", "--state", str(db)])
    monkeypatch.setenv("STONKS_AUTH_PASSWORD", "a different passphrase")
    assert main(["reset-password", "--email", "owner@example.com", "--state", str(db)]) == 0
    with SqliteState(db) as state:
        (row,) = state.sql("SELECT password_hash FROM users WHERE id = ?", [DEFAULT_OWNER_ID])
    assert FAST_HASHER.verify(row["password_hash"], "a different passphrase")
    assert main(["reset-password", "--email", "nobody@example.com", "--state", str(db)]) == 1


def test_weak_password_is_refused(db, monkeypatch, capsys):
    monkeypatch.setenv("STONKS_AUTH_PASSWORD", "short")
    assert main(["bootstrap-admin", "--email", "owner@example.com", "--state", str(db)]) == 1
    assert "at least" in capsys.readouterr().err

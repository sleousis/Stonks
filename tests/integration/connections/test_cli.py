"""``python -m stonks.connections``: list, connect, sync, disconnect and
import-env against a temp state DB with the fake providers."""

from __future__ import annotations

import json

import pytest

from stonks.accounts import DEFAULT_OWNER_ID
from stonks.connections.__main__ import main
from stonks.connections.providers import fake
from stonks.security import generate_key
from stonks.store.state import SqliteState


@pytest.fixture
def env(tmp_path, monkeypatch):
    db = tmp_path / "state.sqlite"
    cfg = tmp_path / "c.toml"
    cfg.write_text(f'[state]\npath = "{db.as_posix()}"\n')
    monkeypatch.setenv("STONKS_SECRET_KEYS", f"k1:{generate_key()}")
    monkeypatch.setenv("STONKS_CONNECTIONS_ENABLED_PROVIDERS", "fake,fake_portal")
    fake.FAKE_BOOKS.clear()
    return ["--config", str(cfg)]


def _run(capsys, *argv) -> tuple[int, str, str]:
    code = main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def test_providers_lists_only_enabled(env, capsys, monkeypatch):
    code, out, _ = _run(capsys, *env, "providers")
    assert code == 0
    assert "fake" in out and "snaptrade" not in out
    monkeypatch.delenv("STONKS_CONNECTIONS_ENABLED_PROVIDERS")
    code, out, _ = _run(capsys, *env, "providers")
    assert "no provider is enabled" in out


def test_connect_sync_list_disconnect(env, capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("STONKS_CONNECT_TOKEN", "cli-token-123456")
    code, out, err = _run(capsys, *env, "connect", "fake", "--label", "Mine")
    assert code == 0, err
    assert "cli-token-123456" not in out + err
    con_id = out.split()[1]
    assert con_id.startswith("con_")

    code, out, _ = _run(capsys, *env, "sync", con_id)
    assert code == 0
    assert "not covered: XYZ123" in out

    code, out, _ = _run(capsys, *env, "--json", "list")
    [row] = json.loads(out)
    assert (row["id"], row["status"], row["last_sync_status"]) == (con_id, "active", "ok")

    code, out, _ = _run(capsys, *env, "accounts", con_id)
    assert "fake-acc-1" in out

    code, out, _ = _run(capsys, *env, "sync", "--due")
    assert code == 0  # nothing due right after a sync

    code, out, err = _run(capsys, *env, "disconnect", con_id)
    assert code == 2 and "--yes" in err
    code, out, _ = _run(capsys, *env, "disconnect", con_id, "--yes")
    assert code == 0
    with SqliteState(tmp_path / "state.sqlite") as s:
        assert s.sql("SELECT COUNT(*) FROM broker_connections")[0][0] == 0


def test_connect_without_credentials_explains(env, capsys):
    code, _, err = _run(capsys, *env, "connect", "fake")
    assert code == 2
    assert "STONKS_CONNECT_TOKEN" in err


def test_portal_connect_prints_the_link(env, capsys):
    code, out, err = _run(
        capsys, *env, "connect", "fake_portal", "--redirect", "https://stonks.example/cb"
    )
    assert code == 0, err
    assert "https://fake-broker.invalid/portal" in out


def test_errors_are_one_line_and_exit_nonzero(env, capsys):
    code, _, err = _run(capsys, *env, "sync", "con_missing")
    assert code == 1
    assert "not found" in err
    code, _, err = _run(capsys, *env, "--as", "usr_nobody", "list")
    assert code == 1


def test_import_env_on_a_simulated_install(env, capsys):
    code, out, _ = _run(capsys, *env, "import-env")
    assert code == 0
    assert "nothing to import" in out


def test_rotate_keys(env, capsys, monkeypatch):
    monkeypatch.setenv("STONKS_CONNECT_TOKEN", "cli-token-abcdef")
    _run(capsys, *env, "connect", "fake")
    import os

    old = os.environ["STONKS_SECRET_KEYS"]
    monkeypatch.setenv("STONKS_SECRET_KEYS", f"k2:{generate_key()},{old}")
    code, out, _ = _run(capsys, *env, "rotate-keys")
    assert code == 0 and "re-sealed 1" in out


def test_default_principal_is_the_owner(env, capsys, monkeypatch):
    monkeypatch.setenv("STONKS_CONNECT_TOKEN", "cli-token-owner1")
    _run(capsys, *env, "connect", "fake")
    code, out, _ = _run(capsys, *env, "--json", "list")
    assert json.loads(out)[0]["user_id"] == DEFAULT_OWNER_ID

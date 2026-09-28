"""CLI tests for `stonks settings`: the console's editable system settings
from the shell, audited as the operator, and picked up by other commands."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from stonks.cli import _settings, app
from stonks.store.state import SqliteState

CONFIG = """
[lake]
path = "data/lake.duckdb"

[state]
path = "data/state.sqlite"

[registry]
artifacts_dir = "data/artifacts"
""".strip()


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("STONKS_DATA_DIR", raising=False)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "default.toml").write_text(CONFIG)
    (tmp_path / "data").mkdir()
    state = SqliteState(tmp_path / "data" / "state.sqlite")
    state.migrate()
    state.close()
    return tmp_path


def test_set_list_and_reset_a_setting(runner, workdir):
    key = "production.risk.max_weight_per_ticker"
    done = runner.invoke(app, ["settings", "set", key, "0.2", "--reason", "tighter"])
    assert done.exit_code == 0, done.output
    assert _settings().production.risk.max_weight_per_ticker == 0.2
    listed = runner.invoke(app, ["settings", "list", "--group", "risk"])
    assert listed.exit_code == 0 and key in listed.output and "service:cli" in listed.output
    back = runner.invoke(app, ["settings", "reset", key, "--reason", "back to the file"])
    assert back.exit_code == 0, back.output
    assert _settings().production.risk.max_weight_per_ticker == 1.0
    state = SqliteState(workdir / "data" / "state.sqlite")
    try:
        actors = {r["actor"] for r in state.sql("SELECT actor FROM audit_log")}
    finally:
        state.close()
    assert actors == {"service:cli"}


def test_bad_values_and_secrets_are_refused(runner, workdir):
    bad = runner.invoke(
        app, ["settings", "set", "production.risk.max_weight_per_ticker", "2", "--reason", "x y z"]
    )
    assert bad.exit_code != 0
    secret = runner.invoke(
        app, ["settings", "set", "sources.eodhd.api_key", "k", "--reason", "abc"]
    )
    assert secret.exit_code != 0

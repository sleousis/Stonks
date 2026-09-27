"""CLI tests for `stonks halts` (roadmap 12.6): list, kill, resume, clear."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from stonks.cli import app
from stonks.production.halts import trip_halt
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


def _rows(workdir, sql: str) -> list:
    state = SqliteState(workdir / "data" / "state.sqlite")
    try:
        return state.sql(sql)
    finally:
        state.close()


def test_list_is_empty_without_halts(runner, workdir):
    result = runner.invoke(app, ["halts", "list"])
    assert result.exit_code == 0, result.output
    assert "no halt in force" in result.output


def test_kill_global_then_list_then_resume_with_the_typed_phrase(runner, workdir):
    result = runner.invoke(app, ["halts", "kill", "--scope", "global", "--reason", "incident"])
    assert result.exit_code == 0, result.output
    [row] = _rows(workdir, "SELECT id, kind, scope, halt, tripped_by FROM risk_halts")
    assert (row["kind"], row["scope"], row["halt"]) == ("kill", "global", "all")
    assert row["tripped_by"] == "service:cli"

    listed = runner.invoke(app, ["halts", "list"])
    assert listed.exit_code == 0 and "kill" in listed.output and "incident" in listed.output

    wrong = runner.invoke(
        app, ["halts", "resume", str(row["id"]), "--reason", "fixed"], input="resume\n"
    )
    assert wrong.exit_code != 0
    assert _rows(workdir, "SELECT cleared_at FROM risk_halts")[0]["cleared_at"] is None

    ok = runner.invoke(
        app, ["halts", "resume", str(row["id"]), "--reason", "fixed"], input="RESUME TRADING\n"
    )
    assert ok.exit_code == 0, ok.output
    [cleared] = _rows(workdir, "SELECT cleared_by, clear_reason FROM risk_halts")
    assert (cleared["cleared_by"], cleared["clear_reason"]) == ("service:cli", "fixed")
    audit = _rows(workdir, "SELECT action FROM audit_log ORDER BY id")
    assert [a["action"] for a in audit][-2:] == ["kill_switch.engage", "kill_switch.resume"]


@pytest.mark.parametrize("flag", ["--buys-only", "--flatten"])
def test_kill_a_portfolio_with_buys_only_stops_buys_only(runner, workdir, flag):
    result = runner.invoke(
        app,
        ["halts", "kill", "--scope", "portfolio", "--portfolio", "pf_default", flag,
         "--reason", "exit"],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    [row] = _rows(workdir, "SELECT portfolio_id, halt FROM risk_halts")
    assert (row["portfolio_id"], row["halt"]) == ("pf_default", "buys")


def test_kill_for_a_user_acts_as_that_user(runner, workdir):
    result = runner.invoke(
        app, ["halts", "kill", "--scope", "user", "--user", "usr_owner", "--reason", "mine"]
    )
    assert result.exit_code == 0, result.output
    [row] = _rows(workdir, "SELECT scope, user_id, tripped_by FROM risk_halts")
    assert (row["scope"], row["user_id"], row["tripped_by"]) == (
        "user",
        "usr_owner",
        "user:usr_owner",
    )


def test_a_user_kill_needs_a_user(runner, workdir):
    result = runner.invoke(app, ["halts", "kill", "--scope", "user", "--reason", "x"])
    assert result.exit_code != 0
    assert _rows(workdir, "SELECT COUNT(*) FROM risk_halts")[0][0] == 0


def test_clear_a_breaker_halt_and_list_all(runner, workdir):
    state = SqliteState(workdir / "data" / "state.sqlite")
    try:
        halt, _ = trip_halt(state, "drawdown", reason="dd", actor="system", portfolio_id="pf_x")
    finally:
        state.close()
    result = runner.invoke(app, ["halts", "clear", str(halt.id), "--reason", "reviewed"])
    assert result.exit_code == 0, result.output
    assert runner.invoke(app, ["halts", "list"]).output.count("drawdown") == 0
    assert "drawdown" in runner.invoke(app, ["halts", "list", "--all"]).output


def test_clear_refuses_the_kill_switch(runner, workdir):
    runner.invoke(app, ["halts", "kill", "--scope", "global", "--reason", "incident"])
    [row] = _rows(workdir, "SELECT id FROM risk_halts")
    result = runner.invoke(app, ["halts", "clear", str(row["id"]), "--reason", "x"])
    assert result.exit_code != 0
    assert "resume" in result.output

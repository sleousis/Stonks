"""CLI tests for `stonks live stage` and `stonks live preview` (roadmap 19.9)."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from stonks.cli import app
from stonks.production.live.stages import change_stage, get_stage, stage_history
from stonks.store.state import SqliteState

CONFIG = """
[lake]
path = "lake.duckdb"

[state]
path = "state.sqlite"

[registry]
artifacts_dir = "artifacts"
""".strip()


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "default.toml").write_text(CONFIG)
    state = SqliteState(tmp_path / "state.sqlite")
    state.migrate()
    state.close()
    return tmp_path


def _state(home) -> SqliteState:
    return SqliteState(home / "state.sqlite")


def test_stage_show_and_report(runner, home):
    shown = runner.invoke(app, ["live", "stage", "show", "pf_default"])
    assert shown.exit_code == 0, shown.output
    assert "sim_paper" in shown.output and "broker_paper" in shown.output
    report = runner.invoke(app, ["live", "stage", "report", "pf_default"])
    assert report.exit_code == 1  # no subscription, no broker: the gate does not pass
    assert "does not pass" in report.output and "paper_days" in report.output


def test_promote_refuses_a_failing_gate(runner, home):
    got = runner.invoke(
        app, ["live", "stage", "promote", "pf_default", "--to", "broker_paper", "--reason", "x"]
    )
    assert got.exit_code == 1
    with _state(home) as state:
        assert get_stage(state, "pf_default") == "sim_paper"


def _broker_portfolio_ready(home) -> str:
    from stonks.accounts import PortfolioRepository, Scope

    with _state(home) as state:
        owner = Scope(user_id="usr_owner", role="admin")
        pid = PortfolioRepository(state).create(owner, name="Live", kind="broker").id
        state.execute(
            "INSERT INTO strategies (id, class_path, params_json, status, created_at, updated_at)"
            " VALUES ('s_live', 'x.Y', '{}', 'active', 'x', 'x')"
        )
        state.execute(
            "INSERT INTO subscriptions (id, user_id, strategy_id, portfolio_id, mode,"
            " paper_days_completed, created_at, updated_at)"
            " VALUES ('sub_live', 'usr_owner', 's_live', ?, 'auto', 20, 'x', 'x')",
            [pid],
        )
    return pid


def test_promote_asks_for_the_stage_name(runner, home):
    pid = _broker_portfolio_ready(home)
    args = ["live", "stage", "promote", pid, "--to", "broker_paper", "--reason", "soak"]
    typo = runner.invoke(app, args, input="live_small\n")
    assert typo.exit_code == 1 and "type broker_paper" in typo.output
    got = runner.invoke(app, args, input="broker_paper\n")
    assert got.exit_code == 0, got.output
    with _state(home) as state:
        assert get_stage(state, pid) == "broker_paper"
        [change] = stage_history(state, pid)
    assert change.actor.startswith("cli") and change.gate_report["passed"] is True


def test_demote_logs_under_the_os_user(runner, home):
    with _state(home) as state:
        for stage in ("broker_paper", "live_small"):
            change_stage(
                state, "pf_default", stage, actor="t", reason="setup",
                gate_report={"target": stage, "passed": True},
            )  # fmt: skip
    got = runner.invoke(
        app,
        ["live", "stage", "demote", "pf_default", "--to", "broker_paper", "--reason", "bad week"],
    )
    assert got.exit_code == 0, got.output
    with _state(home) as state:
        assert get_stage(state, "pf_default") == "broker_paper"
        last = stage_history(state, "pf_default")[0]
    assert last.actor.startswith("cli") and last.reason == "bad week"
    wrong = runner.invoke(
        app, ["live", "stage", "demote", "pf_default", "--to", "live", "--reason", "x"]
    )
    assert wrong.exit_code == 1


def test_preview_of_a_portfolio_without_a_live_book(runner, home):
    got = runner.invoke(app, ["live", "preview", "pf_default"])
    assert got.exit_code == 1
    assert "no live book" in got.output


def test_journal_lists_the_recorded_gateway_calls(runner, tmp_path):
    from stonks.execution.brokers.ibkr.journal import FileJournal, JournalingIbClient
    from tests.fakes.ib_gateway import FakeIbGateway

    client = JournalingIbClient(FakeIbGateway(), FileJournal(tmp_path / "j"))
    client.connect()
    client.managed_accounts()
    got = runner.invoke(app, ["live", "journal", str(tmp_path / "j")])
    assert got.exit_code == 0, got.output
    assert "managed_accounts" in got.output and "2 events, 0 errors" in got.output
    only = runner.invoke(app, ["live", "journal", str(tmp_path / "j"), "--kind", "error"])
    assert "0 events" in only.output
    none = runner.invoke(app, ["live", "journal", str(tmp_path / "nothing.jsonl")])
    assert none.exit_code != 0

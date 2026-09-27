"""``stonks telegram``: link-code, status, unlink and poll."""

from __future__ import annotations

from typer.testing import CliRunner

from stonks.accounts import Role, UserRepository
from stonks.cli import app
from stonks.store.state import SqliteState
from stonks.telegram.links import LinkStore


def test_link_code_status_unlink_and_poll(tmp_path, monkeypatch):
    monkeypatch.setenv("STONKS_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("STONKS_TELEGRAM_BOT_TOKEN", raising=False)
    with SqliteState(tmp_path / "state.sqlite") as state:
        state.migrate()
        uid = (
            UserRepository(state)
            .create(display_name="a", role=Role.TRADER, actor="t", email="a@example.com")
            .id
        )
    runner = CliRunner()
    made = runner.invoke(app, ["telegram", "link-code", "--user", "a@example.com"])
    assert made.exit_code == 0, made.output
    code = made.output.split("code ")[1].split(",")[0].strip()
    with SqliteState(tmp_path / "state.sqlite") as state:
        LinkStore(state).redeem(code, "11", "alice")
    status = runner.invoke(app, ["telegram", "status", "--user", "a@example.com"])
    assert "@alice" in status.output
    assert "unlinked" in runner.invoke(app, ["telegram", "unlink", "--user", uid]).output
    assert runner.invoke(app, ["telegram", "status", "--user", "nobody@x.io"]).exit_code != 0
    poll = runner.invoke(app, ["telegram", "poll", "--once"])
    assert poll.exit_code != 0 and "STONKS_TELEGRAM_BOT_TOKEN" in poll.output

"""CLI tests for `stonks tickets` (roadmap 19.8): list, show, approve and
reject order tickets from the shell. Approving asks for a typed phrase at a
terminal (the shell's stand-in for a fresh second factor), and refuses
without one."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from typer.testing import CliRunner

from stonks import cli_tickets
from stonks.accounts import PortfolioRepository, Role, Scope, UserRepository
from stonks.app.tickets import APPROVE_PHRASE
from stonks.cli import app
from stonks.core.types import Order
from stonks.production.tickets import SubmitWindow, write_tickets
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
    monkeypatch.setenv("COLUMNS", "400")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "default.toml").write_text(CONFIG)
    (tmp_path / "data").mkdir()
    state = SqliteState(tmp_path / "data" / "state.sqlite")
    state.migrate()
    state.close()
    return tmp_path


@pytest.fixture
def terminal(monkeypatch):
    """Pretend the shell is interactive (CliRunner's stdin is not a tty)."""
    monkeypatch.setattr(cli_tickets, "_interactive", lambda: True)


def _state(workdir) -> SqliteState:
    return SqliteState(workdir / "data" / "state.sqlite")


def _person(workdir, email: str, role: Role) -> Scope:
    with _state(workdir) as state:
        user = UserRepository(state).create(display_name=email, role=role, actor="t", email=email)
    return Scope.for_user(user)


def _tickets(workdir, scope: Scope, n: int = 2) -> tuple[str, list[str]]:
    now = datetime.now(UTC)
    with _state(workdir) as state:
        pf = PortfolioRepository(state).create(scope, name="Growth", kind="broker").id
        orders = [
            Order(
                client_id=f"2026-03-17:{pf}:s1:T{i}.US:buy",
                ticker=f"T{i}.US",
                side="buy",
                quantity=5.0,
                strategy_id="s1",
                portfolio_id=pf,
                decision_price=20.0,
                decision_context={"score": 0.1},
            )
            for i in range(n)
        ]
        tickets = write_tickets(
            state,
            orders,
            portfolio_id=pf,
            tick_id=None,
            as_of=date(2026, 3, 17),
            window=SubmitWindow(now - timedelta(minutes=5), now + timedelta(hours=1)),
            hold=lambda _: "approve_mode",
            now=now,
        )
    return pf, [t.id for t in tickets]


def _statuses(workdir) -> dict[str, str]:
    with _state(workdir) as state:
        return {r["id"]: r["status"] for r in state.sql("SELECT id, status FROM order_tickets")}


def test_list_and_show_as_the_operator_and_as_the_owner(runner, workdir):
    alice = _person(workdir, "alice@example.com", Role.TRADER)
    _, ids = _tickets(workdir, alice)

    listed = runner.invoke(app, ["tickets", "list"])
    assert listed.exit_code == 0, listed.output
    assert ids[0] in listed.output and "T1.US" in listed.output and "awaiting" in listed.output

    mine = runner.invoke(app, ["tickets", "list", "--user", "alice@example.com"])
    assert mine.exit_code == 0 and ids[1] in mine.output

    shown = runner.invoke(app, ["tickets", "show", ids[0]])
    assert shown.exit_code == 0, shown.output
    assert "Growth" in shown.output and "approve_mode" in shown.output


def test_someone_else_sees_nothing(runner, workdir):
    alice = _person(workdir, "alice@example.com", Role.TRADER)
    _person(workdir, "bob@example.com", Role.TRADER)
    _, ids = _tickets(workdir, alice)
    listed = runner.invoke(app, ["tickets", "list", "--user", "bob@example.com"])
    assert listed.exit_code == 0 and "no tickets" in listed.output
    hidden = runner.invoke(app, ["tickets", "show", ids[0], "--user", "bob@example.com"])
    assert hidden.exit_code != 0 and "not found" in hidden.output


def test_approve_needs_the_typed_phrase(runner, workdir, terminal):
    alice = _person(workdir, "alice@example.com", Role.TRADER)
    _, ids = _tickets(workdir, alice)

    wrong = runner.invoke(app, ["tickets", "approve", *ids], input="yes\n")
    assert wrong.exit_code != 0 and APPROVE_PHRASE in wrong.output
    assert set(_statuses(workdir).values()) == {"awaiting_approval"}

    ok = runner.invoke(app, ["tickets", "approve", *ids], input=f"{APPROVE_PHRASE}\n")
    assert ok.exit_code == 0, ok.output
    assert "approved 2" in ok.output
    assert set(_statuses(workdir).values()) == {"approved"}
    with _state(workdir) as state:
        actors = {r["actor"] for r in state.sql("SELECT actor FROM audit_log")}
    assert "service:cli" in actors


def test_approve_refuses_without_a_terminal(runner, workdir, monkeypatch):
    monkeypatch.setattr(cli_tickets, "_interactive", lambda: False)
    alice = _person(workdir, "alice@example.com", Role.TRADER)
    _, ids = _tickets(workdir, alice)
    piped = runner.invoke(app, ["tickets", "approve", *ids], input=f"{APPROVE_PHRASE}\n")
    assert piped.exit_code != 0 and "terminal" in piped.output
    assert set(_statuses(workdir).values()) == {"awaiting_approval"}


def test_a_viewer_may_not_approve_or_reject(runner, workdir, terminal):
    viewer = _person(workdir, "vic@example.com", Role.VIEWER)
    _, ids = _tickets(workdir, viewer)
    approve = runner.invoke(
        app,
        ["tickets", "approve", ids[0], "--user", "vic@example.com"],
        input=f"{APPROVE_PHRASE}\n",
    )
    assert approve.exit_code != 0 and "not allowed" in approve.output
    reject = runner.invoke(
        app, ["tickets", "reject", ids[0], "--reason", "no thanks", "--user", "vic@example.com"]
    )
    assert reject.exit_code != 0 and "not allowed" in reject.output
    assert set(_statuses(workdir).values()) == {"awaiting_approval"}


def test_the_owner_approves_their_own_and_rejects_with_a_reason(runner, workdir, terminal):
    alice = _person(workdir, "alice@example.com", Role.TRADER)
    _, ids = _tickets(workdir, alice)
    ok = runner.invoke(
        app,
        ["tickets", "approve", ids[0], "--user", "alice@example.com"],
        input=f"{APPROVE_PHRASE}\n",
    )
    assert ok.exit_code == 0, ok.output

    short = runner.invoke(app, ["tickets", "reject", ids[1], "--reason", "no"])
    assert short.exit_code != 0
    rejected = runner.invoke(
        app, ["tickets", "reject", ids[1], "--reason", "price moved", "--user", "alice@example.com"]
    )
    assert rejected.exit_code == 0, rejected.output
    assert _statuses(workdir) == {ids[0]: "approved", ids[1]: "rejected"}

    again = runner.invoke(app, ["tickets", "approve", ids[1]], input=f"{APPROVE_PHRASE}\n")
    assert again.exit_code != 0

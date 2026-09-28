"""``stonks cash-flows`` and the returns line of ``stonks pnl``."""

from __future__ import annotations

from typer.testing import CliRunner

from stonks.accounts import PortfolioRepository, Role, Scope, UserRepository
from stonks.cli import app
from stonks.store.lake import DuckDBLake
from stonks.store.state import SqliteState


def test_record_list_and_pnl_returns(tmp_path, monkeypatch):
    monkeypatch.setenv("STONKS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COLUMNS", "300")
    with DuckDBLake(tmp_path / "lake.duckdb") as lake:
        lake.migrate()
    with SqliteState(tmp_path / "state.sqlite") as state:
        state.migrate()
        user = UserRepository(state).create(
            display_name="a", role=Role.TRADER, actor="t", email="a@example.com"
        )
        pid = (
            PortfolioRepository(state)
            .create(Scope.for_user(user), name="Mine", initial_cash=1_000.0)
            .id
        )
    runner = CliRunner()
    who = ["--user", "a@example.com"]
    first = runner.invoke(
        app,
        [
            "cash-flows",
            "record",
            "--kind",
            "deposit",
            "--amount",
            "500",
            "--date",
            "2026-03-02",
            *who,
        ],
    )
    assert first.exit_code == 0, first.output
    second = runner.invoke(
        app,
        [
            "cash-flows",
            "record",
            "--kind",
            "deposit",
            "--amount",
            "500",
            "--date",
            "2026-03-09",
            *who,
        ],
    )
    assert second.exit_code == 0, second.output
    listed = runner.invoke(app, ["cash-flows", "list", *who])
    assert listed.output.count("deposit") == 2
    bad = runner.invoke(
        app, ["cash-flows", "record", "--kind", "withdrawal", "--amount", "99999", *who]
    )
    assert bad.exit_code != 0
    pnl = runner.invoke(app, ["pnl", "--portfolio", pid])
    assert pnl.exit_code == 0, pnl.output
    assert "time-weighted +0.00%" in pnl.output and "net deposits +500.00" in pnl.output

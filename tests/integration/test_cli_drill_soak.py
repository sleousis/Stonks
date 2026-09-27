"""CLI tests for `stonks halts drill` and `stonks live soak-report` (roadmap 19.11)."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from stonks.app.drills import reconcile_portfolio, run_kill_switch_drill_scratch
from stonks.cli import app
from stonks.config import Settings, StateConfig
from stonks.core.types import Order
from stonks.execution.brokers.ibkr.broker import IbkrBroker
from stonks.production.drills import SimulatedWorkingBroker
from stonks.store.state import SqliteState
from tests.fakes.ib_gateway import FakeIbGateway

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
    return tmp_path


def test_scratch_drill_goes_through_the_halt_service_and_leaves_real_state_alone(tmp_path):
    real = tmp_path / "real.sqlite"
    settings = Settings(state=StateConfig(path=real))
    report = run_kill_switch_drill_scratch(settings)
    assert report.passed, report.steps
    assert not real.exists()


def test_scratch_drill_through_the_ibkr_adapter_on_the_fake_gateway(tmp_path):
    gw = FakeIbGateway()
    settings = Settings(state=StateConfig(path=tmp_path / "real.sqlite"))
    report = run_kill_switch_drill_scratch(
        settings, broker=IbkrBroker(gw, mode="paper"), reference_price=200.0
    )
    assert report.passed, report.steps
    assert gw.global_cancels == 1


def test_halts_drill_prints_every_step(runner, workdir):
    result = runner.invoke(app, ["halts", "drill"])
    assert result.exit_code == 0, result.output
    for name in ("place_working_order", "engage_kill", "working_order_cancelled", "resume"):
        assert name in result.output
    assert "drill passed" in result.output
    assert not (workdir / "data" / "state.sqlite").exists()


def test_halts_drill_json(runner, workdir):
    out = workdir / "drill.json"
    result = runner.invoke(app, ["halts", "drill", "--json-out", str(out)])
    assert result.exit_code == 0, result.output
    data = json.loads(out.read_text())
    assert data["passed"] is True and data["broker"] == "simulated"


def test_halts_drill_only_knows_the_simulated_broker(runner, workdir):
    result = runner.invoke(app, ["halts", "drill", "--broker", "ibkr"])
    assert result.exit_code != 0


def _seed(workdir) -> None:
    state = SqliteState(workdir / "data" / "state.sqlite")
    state.migrate()
    state.execute(
        "INSERT INTO portfolios (id, owner_id, name, kind, created_at)"
        " VALUES ('pf_ib', 'usr_owner', 'IBKR paper', 'broker', '2026-09-01')"
    )
    state.execute(
        "INSERT INTO orders (client_id, ticker, side, quantity, order_type, status,"
        " created_at, updated_at, portfolio_id, decision_price, decided_at) VALUES"
        " ('o1', 'AAPL.US', 'buy', 1, 'market', 'filled', '2026-09-22T21:00:00',"
        " '2026-09-22T21:00:00', 'pf_ib', 100, '2026-09-22')"
    )
    state.execute(
        "INSERT INTO fills (order_client_id, ticker, quantity, price, fee, filled_at,"
        " portfolio_id) VALUES ('o1', 'AAPL.US', 1, 100.2, 0, '2026-09-23T13:30:00', 'pf_ib')"
    )
    state.close()


def test_live_soak_report_table(runner, workdir):
    _seed(workdir)
    result = runner.invoke(
        app, ["live", "soak-report", "--portfolio", "pf_ib", "--days", "1", "--end", "2026-09-25"]
    )
    assert result.exit_code == 0, result.output
    assert "pf_ib" in result.output
    assert "clean" in result.output


def test_live_soak_report_json_and_exit_code_when_not_clean(runner, workdir):
    _seed(workdir)
    result = runner.invoke(
        app,
        ["live", "soak-report", "--portfolio", "pf_ib", "--days", "20", "--end", "2026-09-25",
         "--json", "--strict"],
    )  # fmt: skip
    assert result.exit_code == 1, result.output
    data = json.loads(result.stdout)
    assert data["days_observed"] == 1 and data["clean"] is False


def test_live_reconcile_without_an_external_broker(runner, workdir):
    result = runner.invoke(app, ["live", "reconcile"])
    assert result.exit_code == 0, result.output
    assert "no external broker" in result.output


def test_reconcile_portfolio_syncs_open_orders(tmp_path):
    path = tmp_path / "state.sqlite"
    state = SqliteState(path)
    state.migrate()
    state.execute(
        "INSERT INTO orders (client_id, ticker, side, quantity, order_type, limit_price, status,"
        " created_at, updated_at, portfolio_id) VALUES ('w1', 'AAPL.US', 'buy', 1, 'limit', 50,"
        " 'pending', '2026-09-25T21:00:00', '2026-09-25T21:00:00', 'pf_default')"
    )
    state.close()
    broker = SimulatedWorkingBroker()
    broker.place_order(Order(client_id="w1", ticker="AAPL.US", side="buy", quantity=1,
                             order_type="limit", limit_price=50.0))  # fmt: skip
    broker.cancel_order("w1")
    out = reconcile_portfolio(
        Settings(state=StateConfig(path=path)), portfolio_id="pf_default", brokers=lambda _: broker
    )
    assert out is not None and out.ok
    state = SqliteState(path)
    assert state.sql("SELECT status FROM orders WHERE client_id = 'w1'")[0]["status"] == "cancelled"
    state.close()

"""CLI tests for ``stonks reconcile`` (roadmap 19.5): list, show and run,
the gateway played by ``FakeIbGateway``."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from stonks.backtest.simulated_broker import SimulatedBroker
from stonks.cli import app
from stonks.core.clock import FakeClock
from stonks.core.types import Portfolio
from stonks.production.live.checks import run_check
from stonks.store.state import SqliteState
from tests.fakes.ib_gateway import AAPL, T0, FakeIbGateway

CONFIG = """
[lake]
path = "data/lake.duckdb"

[state]
path = "data/state.sqlite"

[registry]
artifacts_dir = "data/artifacts"

[notify]
backends = []

[brokers.ibkr.gateways.paper]
host = "ib-gateway-paper"
port = 4004
mode = "paper"
portfolios = ["pf_default"]
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


def _seed_report(workdir, positions) -> str:
    with SqliteState(workdir / "data" / "state.sqlite") as state:
        broker = SimulatedBroker(portfolio=Portfolio(cash=0.0, positions=positions))
        result = run_check(
            state, broker, "pf_default", "sod", clock=FakeClock(T0), publish=lambda e: None
        )
    return result.report.id


def test_list_and_show(runner, workdir):
    assert "no reconcile reports" in runner.invoke(app, ["reconcile", "list"]).output
    report = _seed_report(workdir, {"MSFT.US": 2.0})
    listed = runner.invoke(app, ["reconcile", "list", "--portfolio", "pf_default"])
    assert listed.exit_code == 0 and report in listed.output and "clean" in listed.output
    shown = runner.invoke(app, ["reconcile", "show", report])
    assert shown.exit_code == 0, shown.output
    assert "sod check" in shown.output and "MSFT.US" in shown.output
    missing = runner.invoke(app, ["reconcile", "show", "rec_nope"])
    assert missing.exit_code == 1


def test_run_checks_a_portfolio_through_its_gateway(runner, workdir, monkeypatch):
    import stonks.execution.brokers.ibkr.factory as factory

    gw = FakeIbGateway()
    gw.set_values(NetLiquidation="1000", TotalCashValue="1000")
    gw.set_position(AAPL, 4)
    monkeypatch.setattr(factory, "default_client_factory", lambda endpoint: gw)

    ok = runner.invoke(app, ["reconcile", "run", "--portfolio", "pf_default"])
    assert ok.exit_code == 0, ok.output
    assert "clean" in ok.output

    gw.connect_failures = 10**6
    gw.connected = False
    down = runner.invoke(app, ["reconcile", "run", "--portfolio", "pf_default", "--kind", "sod"])
    assert down.exit_code == 1 and "outage" in down.output


def test_run_refuses_an_unlisted_portfolio_and_a_bad_kind(runner, workdir):
    out = runner.invoke(app, ["reconcile", "run", "--portfolio", "pf_other"])
    assert out.exit_code == 1 and "no IB Gateway lists" in out.output
    bad = runner.invoke(app, ["reconcile", "run", "--portfolio", "pf_default", "--kind", "x"])
    assert bad.exit_code == 2

"""The daily ``ingest_borrow`` scheduler job (roadmap 19.14): IBKR's short
stock files into the lake's ``borrow_rates`` on every backend, skipped
while no IB Gateway is configured. The FTP transport is replaced by canned
files, so nothing touches the network."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

from stonks.config import Settings
from stonks.ingest.sources import ibkr_borrow
from stonks.scheduling.api_backend import API_ACTIONS
from stonks.scheduling.config import default_jobs
from stonks.scheduling.in_process import IN_PROCESS_ACTIONS
from stonks.scheduling.jobs import JobSpec, RunContext, borrow_markets
from stonks.scheduling.local import LOCAL_ACTIONS
from stonks.scheduling.triggers import Fire, SessionTrigger
from stonks.store.lake import DuckDBLake

AT = datetime(2026, 9, 28, 20, 35, tzinfo=UTC)
USA = """#BOF|2026.09.28|09:45:03
#SYM|CUR|NAME|CON|ISIN|REBATERATE|FEERATE|AVAILABLE|
AAPL|USD|APPLE INC|265598|US0378331005|4.57|0.25|>10000000|
#EOF|1
"""
GATEWAY = {"gateways": {"paper": {"host": "127.0.0.1", "port": 4002, "mode": "paper"}}}


@pytest.fixture(autouse=True)
def canned_files(monkeypatch):
    def fetcher(host, user, timeout):
        def fetch(name: str) -> str:
            if name != "usa.txt":
                raise OSError("no such file")
            return USA

        return fetch

    monkeypatch.setattr(ibkr_borrow, "ftp_fetcher", fetcher)


def _settings(tmp_path, *, gateway: bool = True) -> Settings:
    extra: dict[str, Any] = {"brokers": {"ibkr": GATEWAY}} if gateway else {}
    return Settings(
        lake={"path": tmp_path / "lake.duckdb"},
        state={"path": tmp_path / "state.sqlite"},
        notify={"backends": []},
        **extra,
    )


def _ctx(settings: Settings, params: dict[str, Any] | None = None) -> RunContext:
    return RunContext(
        spec=JobSpec(
            "ingest_borrow",
            "ingest_borrow",
            SessionTrigger("XNYS", "close", timedelta(minutes=35)),
            params=params or {},
        ),
        fire=Fire(AT, date(2026, 9, 28), "k"),
        run_id="srun_t",
        now=AT,
        settings=settings,
        notifier=None,  # type: ignore[arg-type]
    )


def test_the_job_is_in_the_default_loop():
    [job] = [j for j in default_jobs() if j.name == "ingest_borrow"]
    assert job.action == "ingest_borrow"


def test_markets_come_from_params_else_the_settings(tmp_path):
    settings = _settings(tmp_path)
    assert borrow_markets(_ctx(settings)) == ["usa"]
    assert borrow_markets(_ctx(settings, {"markets": ["UK", "usa"]})) == ["uk", "usa"]
    with pytest.raises(ValueError, match="short stock market"):
        borrow_markets(_ctx(settings, {"markets": ["mars"]}))


def test_off_without_a_gateway(tmp_path):
    assert borrow_markets(_ctx(_settings(tmp_path, gateway=False))) is None


@pytest.mark.parametrize("registry", [LOCAL_ACTIONS, API_ACTIONS, IN_PROCESS_ACTIONS])
def test_every_backend_skips_without_a_gateway(tmp_path, registry):
    out = registry.get("ingest_borrow")(_ctx(_settings(tmp_path, gateway=False)))
    assert out.status == "skipped" and out.detail["reason"] == "no_gateways"


def test_the_local_job_writes_borrow_rates(tmp_path):
    settings = _settings(tmp_path)
    out = LOCAL_ACTIONS.get("ingest_borrow")(_ctx(settings))
    assert out.status == "succeeded", out.detail
    assert out.detail["tickers_ok"] == 1
    with DuckDBLake(settings.lake.path) as lake:
        row = lake.borrow_rate("AAPL.US", date(2026, 9, 28))
    assert row is not None and row["fee_rate_annual"] == pytest.approx(0.0025)


def test_a_missing_market_soft_fails(tmp_path):
    settings = _settings(tmp_path)
    out = LOCAL_ACTIONS.get("ingest_borrow")(_ctx(settings, {"markets": ["usa", "uk"]}))
    assert out.detail["tickers_ok"] == 1 and out.detail["tickers_failed"] == 1

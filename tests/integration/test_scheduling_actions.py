"""Built-in scheduler actions against real (temporary) stores. Hermetic:
the ingest source is a fake, nothing leaves the process."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta

import pytest

from stonks.config import Settings
from stonks.ingest.schemas import FinancialStatementsBundle, RawPriceBar
from stonks.ingest.sources.base import DataSource
from stonks.notify import Notification, Notifier
from stonks.scheduling import local as jobs_mod
from stonks.scheduling.jobs import JobSpec, RunContext
from stonks.scheduling.local import LOCAL_ACTIONS
from stonks.scheduling.triggers import Fire, SessionTrigger
from stonks.store.lake import DuckDBLake
from stonks.store.state import SqliteState

get_action = LOCAL_ACTIONS.get


class Recorder(Notifier):
    def __init__(self) -> None:
        self.sent: list[Notification] = []

    def _send(self, n: Notification) -> None:
        self.sent.append(n)


class FakeSource(DataSource):
    source_id = "fake"

    def __init__(self) -> None:
        self.calls: list[tuple[str, date | None, date | None]] = []

    def list_tickers(self, exchange: str) -> list[str]:
        return []

    def fetch_prices(
        self, ticker: str, since: date | None = None, until: date | None = None
    ) -> Iterable[RawPriceBar]:
        self.calls.append((ticker, since, until))
        return [
            RawPriceBar(
                ticker=ticker, date=until, open=1, high=1, low=1, close=1, adj_close=1, volume=10
            )
        ]

    def fetch_fundamentals(self, ticker: str) -> FinancialStatementsBundle:
        return FinancialStatementsBundle()


@pytest.fixture
def settings(tmp_path) -> Settings:
    s = Settings(
        lake={"path": tmp_path / "lake.duckdb"},
        state={"path": tmp_path / "state.sqlite"},
        registry={"artifacts_dir": tmp_path / "artifacts"},
        production={"universe": ["AAPL.US", "MSFT.US"]},
        notify={"backends": []},
    )
    with DuckDBLake(s.lake.path) as lake:
        lake.migrate()
    with SqliteState(s.state.path) as state:
        state.migrate()
    return s


def _ctx(settings, action: str, as_of: date, **params) -> tuple[RunContext, Recorder]:
    notifier = Recorder()
    spec = JobSpec(action, action, SessionTrigger("XNYS"), params=params)
    at = datetime(as_of.year, as_of.month, as_of.day, 21, tzinfo=UTC)
    ctx = RunContext(
        spec=spec,
        fire=Fire(at, as_of, as_of.isoformat()),
        run_id="srun_test",
        now=at,
        settings=settings,
        notifier=notifier,
    )
    return ctx, notifier


def _tick_rows(settings) -> list:
    with SqliteState(settings.state.path) as s:
        return s.sql("SELECT id, status FROM tick_runs")


def test_tick_skips_when_the_whole_universe_is_closed(settings):
    ctx, _ = _ctx(settings, "tick", date(2026, 11, 26))  # Thanksgiving
    out = get_action("tick")(ctx)
    assert out.status == "skipped" and out.detail["reason"] == "market_closed"
    assert _tick_rows(settings) == []


def test_tick_runs_on_a_trading_day_for_the_fire_date(settings):
    ctx, _ = _ctx(settings, "tick", date(2026, 9, 25))
    out = get_action("tick")(ctx)
    assert out.status == "succeeded"
    assert out.detail["tick_id"].startswith("tick_2026-09-25_")
    assert len(_tick_rows(settings)) == 1


def test_tick_with_crypto_in_the_universe_runs_on_weekends(settings):
    ctx, _ = _ctx(settings, "tick", date(2026, 9, 26), tickers=["AAPL.US", "BTC-USD.CC"])
    assert get_action("tick")(ctx).status == "succeeded"


def test_tick_skip_can_be_disabled(settings):
    ctx, _ = _ctx(settings, "tick", date(2026, 11, 26), skip_closed_days=False)
    assert get_action("tick")(ctx).status == "succeeded"


def test_ingest_prices_for_the_lookback_window(settings, monkeypatch):
    source = FakeSource()
    monkeypatch.setattr(jobs_mod, "build_source", lambda sid, cfg: source)
    as_of = date(2026, 9, 25)
    ctx, _ = _ctx(settings, "ingest_prices", as_of, lookback_days=3)
    out = get_action("ingest_prices")(ctx)
    assert out.status == "succeeded" and out.detail["tickers_ok"] == 2
    assert source.calls[0] == ("AAPL.US", as_of - timedelta(days=3), as_of)
    with DuckDBLake(settings.lake.path) as lake:
        assert len(lake.sql("SELECT * FROM prices")) == 2


def test_ingest_skips_closed_days(settings, monkeypatch):
    source = FakeSource()
    monkeypatch.setattr(jobs_mod, "build_source", lambda sid, cfg: source)
    ctx, _ = _ctx(settings, "ingest_prices", date(2026, 9, 26))  # Saturday
    assert get_action("ingest_prices")(ctx).status == "skipped"
    assert source.calls == []


def test_health_failure_alerts_itself(settings):
    ctx, notifier = _ctx(settings, "health", date(2026, 9, 25))
    out = get_action("health")(ctx)  # no bars at all -> stale
    assert out.status == "failed" and out.alerted
    assert "freshness:AAPL.US" in out.detail["failed_checks"]
    assert "risk_halts" in out.detail["failed_checks"]  # the operational halt it opened
    assert len(notifier.sent) == 1


def test_report_writes_html(settings, tmp_path):
    out_path = tmp_path / "r" / "report.html"
    ctx, _ = _ctx(settings, "report", date(2026, 9, 25), out=str(out_path))
    out = get_action("report")(ctx)
    assert out.status == "succeeded"
    assert out_path.read_text(encoding="utf-8").lstrip().lower().startswith("<!doctype html")

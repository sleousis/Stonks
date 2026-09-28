"""CLI tests for ``stonks calendars`` (roadmap 20.7): refresh from a fake
source, then the window, news and the earnings check."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pandas as pd
import pytest
from typer.testing import CliRunner

from stonks.app.context import AppContext
from stonks.cli import app
from stonks.ingest.calendar_schemas import DividendEventRow, EarningsEventRow, EconomicEventRow
from stonks.store.lake import DuckDBLake
from tests.fixtures.calendars import FakeCalendarSource

CONFIG = """
[lake]
path = "data/lake.duckdb"

[state]
path = "data/state.sqlite"

[registry]
artifacts_dir = "data/artifacts"
""".strip()

TODAY = datetime.now(UTC).date()


def _day(offset: int) -> date:
    return TODAY + timedelta(days=offset)


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def source(monkeypatch) -> FakeCalendarSource:
    fake = FakeCalendarSource(
        earnings=[
            EarningsEventRow(
                ticker="UP.US", period_end=_day(-30), report_date=_day(2), eps_estimate=1.5
            )
        ],
        dividends=[DividendEventRow(ticker="KO.US", ex_date=_day(3), amount=0.5)],
        economic=[
            EconomicEventRow(
                country="US",
                event_time=datetime.combine(_day(1), datetime.min.time(), tzinfo=UTC),
                event_type="CPI",
                comparison="yoy",
                estimate=2.5,
            )
        ],
    )
    monkeypatch.setattr(AppContext, "build_source", lambda self, source_id=None: fake)
    return fake


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("STONKS_DATA_DIR", raising=False)
    monkeypatch.setenv("COLUMNS", "240")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "default.toml").write_text(CONFIG)
    (tmp_path / "data").mkdir()
    return tmp_path


def test_refresh_then_show(runner, workdir, source):
    res = runner.invoke(app, ["calendars", "refresh", "--no-alerts"])
    assert res.exit_code == 0, res.output
    assert "3 ok, 0 failed" in res.output
    shown = runner.invoke(app, ["calendars", "show", "--scope", "all"])
    assert shown.exit_code == 0, shown.output
    assert "UP.US" in shown.output and "KO.US" in shown.output and "CPI" in shown.output
    picked = runner.invoke(app, ["calendars", "show", "--scope", "tickers", "--tickers", "ko.us"])
    assert "KO.US" in picked.output and "UP.US" not in picked.output


def test_refresh_sends_alerts_by_default(runner, workdir, source):
    res = runner.invoke(app, ["calendars", "refresh"])
    assert res.exit_code == 0, res.output
    assert "event alerts sent" in res.output


def test_bad_input_is_a_usage_error(runner, workdir, source):
    res = runner.invoke(app, ["calendars", "show", "--scope", "tickers"])
    assert res.exit_code == 2
    res = runner.invoke(app, ["calendars", "show", "--start", "nope"])
    assert res.exit_code == 2
    res = runner.invoke(app, ["calendars", "show", "--user", "ghost@example.com"])
    assert res.exit_code == 2


def test_news_and_earnings_check(runner, workdir, source):
    runner.invoke(app, ["calendars", "refresh", "--no-alerts"])
    with DuckDBLake(workdir / "data" / "lake.duckdb") as lake:
        lake.upsert_news(
            pd.DataFrame(
                [
                    {
                        "ticker": "UP.US",
                        "published_at": datetime.now(UTC).replace(tzinfo=None),
                        "title": "Up Corp beats",
                        "url": None,
                        "source_name": "Wire",
                        "content": None,
                        "symbols": ["UP.US"],
                        "tags": [],
                        "sentiment": 0.8,
                        "sentiment_pos": None,
                        "sentiment_neg": None,
                        "sentiment_neu": None,
                    }
                ]
            )
        )
    news = runner.invoke(app, ["calendars", "news", "--scope", "tickers", "--tickers", "UP.US"])
    assert news.exit_code == 0, news.output
    assert "Up Corp beats" in news.output
    refused = runner.invoke(app, ["calendars", "news", "--scope", "all"])
    assert refused.exit_code == 2
    check = runner.invoke(app, ["calendars", "earnings-check", "KO.US"])
    assert check.exit_code == 0, check.output
    assert "no earnings before the next open" in check.output

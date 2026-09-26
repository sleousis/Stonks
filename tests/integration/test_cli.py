"""Smoke tests for the Typer CLI. Uses a fake source injected via a monkeypatch
so we exercise the full wiring without touching the network."""

from __future__ import annotations

from datetime import date

import pytest
from typer.testing import CliRunner

from stonks.cli import app
from stonks.ingest.schemas import RawPriceBar
from stonks.ingest.sources.base import DataSource


class _FakeSource(DataSource):
    source_id = "eodhd"

    def list_tickers(self, exchange):
        return ["AAPL.US"]

    def fetch_prices(self, ticker, since=None, until=None):
        return [
            RawPriceBar(
                ticker=ticker,
                date=date(2026, 4, 1),
                open=100.0,
                high=105.0,
                low=99.0,
                close=104.0,
                adj_close=104.0,
                volume=1_000_000,
            )
        ]

    def fetch_fundamentals(self, ticker):
        from stonks.ingest.schemas import FinancialStatementsBundle

        return FinancialStatementsBundle()


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "default.toml").write_text(
        """
[lake]
path = "data/lake.duckdb"

[state]
path = "data/state.sqlite"

[sources.eodhd]
base_url = "https://example.test/api"
""".strip()
    )
    monkeypatch.setenv("EODHD_API_KEY", "test-key")
    return tmp_path


def test_db_init_creates_lake_and_state(runner, cli_env):
    result = runner.invoke(app, ["db", "init"])
    assert result.exit_code == 0, result.output
    assert (cli_env / "data" / "lake.duckdb").exists()
    assert (cli_env / "data" / "state.sqlite").exists()


def test_db_info_lists_tables_from_both_stores(runner, cli_env):
    runner.invoke(app, ["db", "init"])
    result = runner.invoke(app, ["db", "info"])
    assert result.exit_code == 0, result.output
    # lake tables
    assert "prices" in result.output
    assert "income_statement" in result.output
    assert "balance_sheet" in result.output
    assert "cash_flow_statement" in result.output
    assert "ingest_runs" in result.output
    # state tables
    assert "strategies" in result.output
    assert "orders" in result.output
    assert "tick_runs" in result.output


def test_ingest_prices_via_fake_source(runner, cli_env, monkeypatch):
    from stonks import cli as cli_module

    monkeypatch.setattr(cli_module, "_build_source", lambda settings: _FakeSource())

    runner.invoke(app, ["db", "init"])
    result = runner.invoke(
        app,
        ["ingest", "prices", "--tickers", "AAPL.US", "--since", "2026-03-01"],
    )
    assert result.exit_code == 0, result.output
    assert "ok" in result.output.lower()


@pytest.mark.parametrize(
    "args",
    [
        ["ingest", "prices", "--tickers", "AAPL.US", "--since", "2026-13-01"],
        ["ingest", "prices", "--tickers", "AAPL.US", "--until", "yesterday"],
        ["ingest", "intraday", "--tickers", "AAPL.US", "--since", "03/01/2026"],
        ["ingest", "all-intervals", "--tickers", "AAPL.US", "--intraday-since", "nope"],
        ["tick", "--as-of", "2026-02-30"],
    ],
)
def test_bad_date_options_give_usage_error_not_traceback(runner, cli_env, monkeypatch, args):
    from stonks import cli as cli_module

    monkeypatch.setattr(cli_module, "_build_source", lambda settings: _FakeSource())
    runner.invoke(app, ["db", "init"])
    result = runner.invoke(app, args)
    assert result.exit_code == 2, result.output
    assert "YYYY-MM-DD" in result.output
    assert not isinstance(result.exception, ValueError)


def test_ingest_metadata_via_fake_source(runner, cli_env, monkeypatch):
    """`stonks ingest metadata` runs against a FakeSource that returns a
    minimal non-empty MetadataBundle; the run should report ok."""
    from datetime import date as _date

    from stonks import cli as cli_module
    from stonks.ingest.metadata_bundle import MetadataBundle
    from stonks.ingest.schemas import DividendRow, TickerProfile

    class _FakeMetaSource(_FakeSource):
        def fetch_metadata(self, ticker):
            return MetadataBundle(
                profile=TickerProfile(id=ticker, name="Apple Inc", sector="Technology"),
                dividends=(
                    DividendRow(
                        ticker=ticker,
                        ex_date=_date(2026, 2, 10),
                        amount=0.25,
                        currency="USD",
                    ),
                ),
            )

    monkeypatch.setattr(cli_module, "_build_source", lambda settings: _FakeMetaSource())

    runner.invoke(app, ["db", "init"])
    result = runner.invoke(app, ["ingest", "metadata", "--tickers", "AAPL.US"])
    assert result.exit_code == 0, result.output
    assert "ok" in result.output.lower()
    assert "metadata" in result.output


def test_ingest_exchanges_exits_cleanly_on_free_tier(runner, cli_env, monkeypatch):
    """I7: a free-tier user invoking ``stonks ingest exchanges`` should see
    a friendly message + non-zero exit, not a Python traceback."""
    from stonks import cli as cli_module
    from stonks.ingest.sources.eodhd import EodhdFreeTierError

    class _FreeTierSource(_FakeSource):
        def list_exchanges(self):
            raise EodhdFreeTierError("exchanges-list endpoint requires a paid plan")

    monkeypatch.setattr(cli_module, "_build_source", lambda settings: _FreeTierSource())

    result = runner.invoke(app, ["ingest", "exchanges"])
    assert result.exit_code == 2, result.output
    assert "free tier" in result.output.lower()
    # Make sure we did NOT spill a Traceback to the user.
    assert "Traceback" not in result.output


def test_ingest_macro_via_fake_source(runner, cli_env, monkeypatch):
    """`stonks ingest macro --countries USA --indicators real_gdp_total`
    runs end-to-end against a fake source that returns one observation."""
    from datetime import date as _date

    from stonks import cli as cli_module
    from stonks.ingest.schemas import MacroIndicatorRow

    class _FakeMacroSource(_FakeSource):
        def fetch_macro_indicator(self, country_iso, indicator):
            return [
                MacroIndicatorRow(
                    country_iso=country_iso,
                    indicator=indicator,
                    observation_date=_date(2024, 1, 1),
                    period="annual",
                    value=27_000.0,
                ),
            ]

    monkeypatch.setattr(cli_module, "_build_source", lambda settings: _FakeMacroSource())

    runner.invoke(app, ["db", "init"])
    result = runner.invoke(
        app,
        ["ingest", "macro", "--countries", "USA", "--indicators", "real_gdp_total"],
    )
    assert result.exit_code == 0, result.output
    assert "ok" in result.output.lower()


def test_ingest_macro_rejects_non_iso3_country(runner, cli_env, monkeypatch):
    """A two-letter country code should bounce at the CLI boundary with a
    BadParameter exit, not bubble through to the HTTP client."""
    from stonks import cli as cli_module

    monkeypatch.setattr(cli_module, "_build_source", lambda settings: _FakeSource())

    runner.invoke(app, ["db", "init"])
    result = runner.invoke(
        app,
        ["ingest", "macro", "--countries", "US", "--indicators", "real_gdp_total"],
    )
    assert result.exit_code != 0
    assert "ISO" in result.output or "alpha-3" in result.output


def test_ingest_macro_defaults_to_vendor_default_indicator(runner, cli_env, monkeypatch):
    """Omitting --indicators should fall back to the vendor's default
    (``gdp_current_usd``) and still produce one successful pair."""
    from datetime import date as _date

    from stonks import cli as cli_module
    from stonks.ingest.schemas import MacroIndicatorRow

    captured: list[tuple[str, str]] = []

    class _CapturingSource(_FakeSource):
        def fetch_macro_indicator(self, country_iso, indicator):
            captured.append((country_iso, indicator))
            return [
                MacroIndicatorRow(
                    country_iso=country_iso,
                    indicator=indicator,
                    observation_date=_date(2024, 1, 1),
                    value=1.0,
                ),
            ]

    monkeypatch.setattr(cli_module, "_build_source", lambda settings: _CapturingSource())

    runner.invoke(app, ["db", "init"])
    result = runner.invoke(app, ["ingest", "macro", "--countries", "USA"])
    assert result.exit_code == 0, result.output
    assert captured == [("USA", "gdp_current_usd")]


def test_ingest_exchanges_exits_with_error_on_request_failure(runner, cli_env, monkeypatch):
    """I7: transport errors should also be caught and shown as a user-facing
    error message rather than a stack trace."""
    import requests

    from stonks import cli as cli_module

    class _FailingSource(_FakeSource):
        def list_exchanges(self):
            raise requests.ConnectionError("DNS failure")

    monkeypatch.setattr(cli_module, "_build_source", lambda settings: _FailingSource())

    result = runner.invoke(app, ["ingest", "exchanges"])
    assert result.exit_code == 1, result.output
    assert "exchanges fetch failed" in result.output.lower()
    assert "Traceback" not in result.output

"""CLI tests for the multi-asset ergonomics on ``stonks ingest`` subcommands.

The goal of these tests is to pin the user-facing surface that lets an
operator say "ingest all the crypto" without first having to learn EODHD's
virtual-exchange codes (CC / COMM / GBOND). The wiring is built on top of
the existing ``classify_asset_class`` + the per-class lake tables landed in
migrations 007 / 008; these tests just exercise the CLI plumbing.

All tests use a fake DataSource so no network is touched.
"""

from __future__ import annotations

from datetime import date

import pytest
from typer.testing import CliRunner

from stonks.cli import app
from stonks.ingest.metadata_bundle import MetadataBundle
from stonks.ingest.schemas import (
    BondProfileRow,
    CommodityContractRow,
    CryptoProfileRow,
    RawPriceBar,
    TickerProfile,
)
from stonks.ingest.sources.base import DataSource


class _FakeMultiAssetSource(DataSource):
    """Records every call made by the CLI so tests can assert on the
    selection logic — which exchange got resolved, which tickers got
    fetched. Returns one synthetic price bar per ticker so the run
    completes successfully through the lake upsert."""

    source_id = "eodhd"

    def __init__(self) -> None:
        self.list_tickers_calls: list[str] = []
        self.fetch_prices_calls: list[str] = []
        self.fetch_metadata_calls: list[str] = []
        # Map exchange code → ticker list. Tests pre-populate the codes
        # they expect the CLI to resolve --asset-class to.
        self.exchange_universe: dict[str, list[str]] = {
            "CC": ["BTC-USD.CC", "ETH-USD.CC"],
            "COMM": ["GC.COMM"],
            "GBOND": ["US10Y.GBOND"],
            "US": ["AAPL.US"],
        }

    def list_tickers(self, exchange):
        self.list_tickers_calls.append(exchange)
        return list(self.exchange_universe.get(exchange, []))

    def fetch_prices(self, ticker, since=None, until=None):
        self.fetch_prices_calls.append(ticker)
        return [
            RawPriceBar(
                ticker=ticker,
                date=date(2026, 4, 1),
                open=100.0,
                high=105.0,
                low=99.0,
                close=104.0,
                adj_close=104.0,
                volume=1_000,
            )
        ]

    def fetch_fundamentals(self, ticker):
        from stonks.ingest.schemas import FinancialStatementsBundle

        return FinancialStatementsBundle()

    def fetch_metadata(self, ticker):
        self.fetch_metadata_calls.append(ticker)
        # Build a class-appropriate bundle so the upsert path covers each
        # of the per-class tables. The non-equity bundles populate
        # exactly the fields ``_fetch_metadata_non_equity`` would.
        if ticker.endswith(".CC"):
            return MetadataBundle(
                profile=TickerProfile(id=ticker, asset_class="crypto"),
                crypto_profile=CryptoProfileRow(ticker=ticker, base_symbol="X", quote_symbol="USD"),
            )
        if ticker.endswith(".COMM"):
            return MetadataBundle(
                profile=TickerProfile(id=ticker, asset_class="commodity"),
                commodity_contract=CommodityContractRow(ticker=ticker, contract_kind="continuous"),
            )
        if ticker.endswith(".GBOND"):
            return MetadataBundle(
                profile=TickerProfile(id=ticker, asset_class="bond"),
                bond_profile=BondProfileRow(ticker=ticker, issuer_kind="sovereign"),
            )
        return MetadataBundle(profile=TickerProfile(id=ticker))


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def cli_env(tmp_path, monkeypatch, runner):
    """Chdir into a temp working dir, drop a minimal config, set the
    fake API key, and provision the lake + state DBs via ``db init``.

    Asserting ``db init`` succeeds here (not in each test) means a
    regression in `db init` itself surfaces with a clear message
    instead of cascading into confusing downstream "exit_code != 0"
    failures in every test that follows.
    """
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
    init_result = runner.invoke(app, ["db", "init"])
    assert init_result.exit_code == 0, init_result.output
    return tmp_path


@pytest.fixture
def fake_source(monkeypatch):
    src = _FakeMultiAssetSource()
    from stonks import cli as cli_module

    monkeypatch.setattr(cli_module, "_build_source", lambda settings, source_id="eodhd": src)
    return src


# ---- ingest prices ---------------------------------------------------------


def test_ingest_prices_asset_class_resolves_to_virtual_exchange(runner, cli_env, fake_source):
    """``--asset-class crypto`` alone (no --tickers, no --exchange) resolves
    to EODHD's ``CC`` virtual exchange and fetches the universe from there."""
    result = runner.invoke(app, ["ingest", "prices", "--asset-class", "crypto"])
    assert result.exit_code == 0, result.output
    assert fake_source.list_tickers_calls == ["CC"]
    assert sorted(fake_source.fetch_prices_calls) == ["BTC-USD.CC", "ETH-USD.CC"]


def test_ingest_prices_asset_class_commodity(runner, cli_env, fake_source):
    result = runner.invoke(app, ["ingest", "prices", "--asset-class", "commodity"])
    assert result.exit_code == 0, result.output
    assert fake_source.list_tickers_calls == ["COMM"]
    assert fake_source.fetch_prices_calls == ["GC.COMM"]


def test_ingest_prices_asset_class_bond(runner, cli_env, fake_source):
    result = runner.invoke(app, ["ingest", "prices", "--asset-class", "bond"])
    assert result.exit_code == 0, result.output
    assert fake_source.list_tickers_calls == ["GBOND"]
    assert fake_source.fetch_prices_calls == ["US10Y.GBOND"]


def test_ingest_prices_asset_class_equity_requires_explicit_universe(runner, cli_env, fake_source):
    """``--asset-class equity`` cannot resolve to a single exchange (there
    are dozens — US, LSE, XETRA, …) so the CLI must reject this combination
    rather than silently picking one. The user is told to pass --exchange
    or --tickers explicitly."""
    result = runner.invoke(app, ["ingest", "prices", "--asset-class", "equity"])
    assert result.exit_code != 0
    # Don't pin the exact message — pin the user's understanding.
    out = result.output.lower()
    assert "equity" in out and ("exchange" in out or "tickers" in out)


def test_ingest_prices_explicit_exchange_overrides_asset_class_resolution(
    runner, cli_env, fake_source
):
    """When the operator passes both ``--asset-class crypto`` and
    ``--exchange US``, the explicit ``--exchange`` wins (the auto-resolver
    only kicks in when ``--exchange`` is omitted). Pin this so a future
    refactor of the universe-resolution logic doesn't silently flip the
    precedence."""
    result = runner.invoke(app, ["ingest", "prices", "--asset-class", "crypto", "--exchange", "US"])
    assert result.exit_code == 0, result.output
    # The fake's ``"US"`` bucket carries AAPL.US — proves the explicit
    # --exchange was used to discover the universe rather than CC.
    assert fake_source.list_tickers_calls == ["US"]
    assert fake_source.fetch_prices_calls == ["AAPL.US"]


def test_ingest_prices_asset_class_validates_explicit_tickers(runner, cli_env, fake_source):
    """Passing both --tickers and --asset-class should validate every
    ticker's classification matches. This catches a copy-paste where the
    operator mixes classes in one batch and would otherwise have the
    instruments table inserted with mismatched asset_class metadata."""
    result = runner.invoke(
        app,
        [
            "ingest",
            "prices",
            "--asset-class",
            "crypto",
            "--tickers",
            "BTC-USD.CC,AAPL.US",
        ],
    )
    assert result.exit_code != 0
    out = result.output.lower()
    assert "aapl.us" in out  # name the offending ticker
    assert "crypto" in out  # name the requested class


def test_ingest_prices_strips_whitespace_around_ticker_separators(runner, cli_env, fake_source):
    """Operators paste ticker lists from spreadsheets / chat / docs;
    the parser must tolerate ``\" BTC-USD.CC , ETH-USD.CC \"`` rather
    than treating the inner spaces as part of the ticker (which would
    otherwise misclassify ``\" BTC-USD.CC\"`` as equity by suffix and
    fail the asset-class validation for spurious reasons)."""
    result = runner.invoke(
        app,
        [
            "ingest",
            "prices",
            "--asset-class",
            "crypto",
            "--tickers",
            " BTC-USD.CC , ETH-USD.CC ",
        ],
    )
    assert result.exit_code == 0, result.output
    assert sorted(fake_source.fetch_prices_calls) == ["BTC-USD.CC", "ETH-USD.CC"]


def test_ingest_prices_rejects_unknown_asset_class(runner, cli_env, fake_source):
    """Typer's callback validation should reject any value outside the
    closed AssetClass set — so ``--asset-class forex`` (a real EODHD
    bucket but not yet in our type) fails fast with a friendly message."""
    result = runner.invoke(
        app, ["ingest", "prices", "--asset-class", "forex", "--tickers", "EURUSD.FOREX"]
    )
    assert result.exit_code != 0
    assert "asset" in result.output.lower() or "forex" in result.output.lower()


# ---- ingest metadata -------------------------------------------------------


def test_ingest_metadata_asset_class_resolves_universe(runner, cli_env, fake_source):
    """``ingest metadata --asset-class crypto`` covers the same universe-
    discovery path as ``ingest prices`` so the operator can populate
    profiles for an entire class without naming each ticker."""
    result = runner.invoke(app, ["ingest", "metadata", "--asset-class", "crypto"])
    assert result.exit_code == 0, result.output
    assert fake_source.list_tickers_calls == ["CC"]
    assert sorted(fake_source.fetch_metadata_calls) == ["BTC-USD.CC", "ETH-USD.CC"]


def test_ingest_metadata_asset_class_validates_explicit_tickers(runner, cli_env, fake_source):
    result = runner.invoke(
        app,
        [
            "ingest",
            "metadata",
            "--asset-class",
            "bond",
            "--tickers",
            "US10Y.GBOND,AAPL.US",
        ],
    )
    assert result.exit_code != 0
    out = result.output.lower()
    assert "aapl.us" in out
    assert "bond" in out


# ---- ingest fundamentals ---------------------------------------------------


def test_ingest_fundamentals_warns_on_non_equity_class(runner, cli_env, fake_source):
    """Fundamentals (income / balance / cashflow) are equity-only — the
    EODHD adapter already short-circuits for non-equity tickers, but
    invoking the command for a clearly-non-equity universe should fail
    fast instead of silently churning through a no-op run."""
    result = runner.invoke(
        app,
        [
            "ingest",
            "fundamentals",
            "--tickers",
            "BTC-USD.CC",
        ],
    )
    assert result.exit_code != 0
    out = result.output.lower()
    assert "fundamentals" in out
    assert "equity" in out


def test_ingest_fundamentals_rejects_non_equity_asset_class_flag(runner, cli_env, fake_source):
    """``--asset-class crypto`` on the equity-only fundamentals command
    must produce the curated equity-only error message rather than
    Typer's default "no such option". Pins the symmetry with the prices
    / metadata commands: every ingest subcommand accepts --asset-class,
    fundamentals just rejects the non-equity values."""
    result = runner.invoke(
        app,
        [
            "ingest",
            "fundamentals",
            "--asset-class",
            "crypto",
            "--tickers",
            "AAPL.US",
        ],
    )
    assert result.exit_code != 0
    out = result.output.lower()
    assert "equity-only" in out or "equity only" in out
    assert "crypto" in out

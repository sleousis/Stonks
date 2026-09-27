"""Fixtures for the service layer and REST API: a tmp workspace with a seeded
lake + state, a fake data source, and a ready ``Services`` container."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date

import pandas as pd
import pytest

from stonks.app.context import AppContext
from stonks.app.services import Services
from stonks.config import ApiConfig, LakeConfig, RegistryConfig, Settings, StateConfig
from stonks.core.protocols import SurvivalReport
from stonks.ingest.pipeline import _rows_to_df
from stonks.ingest.schemas import FinancialStatementsBundle, RawPriceBar, TickerProfile
from stonks.ingest.sources.base import DataSource
from stonks.production.tick import TickSettings, run_tick
from stonks.registry.store import StrategyRegistry
from stonks.store.lake import DuckDBLake
from stonks.store.state import SqliteState
from stonks.strategies.examples.buy_and_hold import BuyAndHold
from tests.fixtures.governance import seed_status

API_TOKEN = "test-token-123"


class FakeDataSource(DataSource):
    source_id = "fake"

    def __init__(self, prices: dict[str, list[RawPriceBar]] | None = None) -> None:
        self._prices = prices or {}

    def list_tickers(self, exchange: str) -> list[str]:
        return sorted(self._prices)

    def fetch_prices(
        self, ticker: str, since: date | None = None, until: date | None = None
    ) -> Iterable[RawPriceBar]:
        return list(self._prices.get(ticker, []))

    def fetch_fundamentals(self, ticker: str) -> FinancialStatementsBundle:
        return FinancialStatementsBundle()


def _seed_lake(path) -> None:
    lake = DuckDBLake(path)
    lake.migrate()
    dates = pd.bdate_range(start="2025-10-01", end="2026-04-01")
    n = len(dates)
    rows = []
    for ticker, closes in [
        ("UP.US", [100.0 + 100.0 * i / (n - 1) for i in range(n)]),
        ("FLAT.US", [50.0] * n),
        ("DOWN.US", [100.0 - 40.0 * i / (n - 1) for i in range(n)]),
    ]:
        for d, c in zip(dates, closes, strict=True):
            rows.append(
                {
                    "ticker": ticker,
                    "date": d.date(),
                    "open": c,
                    "high": c + 0.5,
                    "low": c - 0.5,
                    "close": c,
                    "adj_close": c,
                    "volume": 1_000_000,
                }
            )
    lake.upsert_prices(pd.DataFrame(rows))
    lake.upsert_instrument_profile(
        _rows_to_df(
            [
                TickerProfile(id="UP.US", name="Up Corp", asset_class="equity"),
                TickerProfile(id="FLAT.US", name="Flat Inc", asset_class="equity"),
                TickerProfile(id="DOWN.US", name="Down Ltd", asset_class="equity"),
            ]
        )
    )
    lake.close()


@pytest.fixture
def settings(tmp_path, monkeypatch) -> Settings:
    monkeypatch.setenv("STONKS_API_TOKEN", API_TOKEN)
    return Settings(
        lake=LakeConfig(path=tmp_path / "lake.duckdb"),
        state=StateConfig(path=tmp_path / "state.sqlite"),
        registry=RegistryConfig(artifacts_dir=tmp_path / "artifacts"),
        # Like the dev profile: loopback reads need no credential.
        api=ApiConfig(ui_dist=tmp_path / "no-dist", open_reads_on_loopback=True),
    )


@pytest.fixture
def fake_source() -> FakeDataSource:
    return FakeDataSource(
        prices={
            "NEW.US": [
                RawPriceBar(
                    ticker="NEW.US",
                    date=date(2026, 4, d),
                    open=10.0,
                    high=11.0,
                    low=9.0,
                    close=10.5,
                    adj_close=10.5,
                    volume=100,
                )
                for d in (1, 2, 3)
            ]
        }
    )


@pytest.fixture
def seeded(settings) -> dict:
    """Seed prices, profiles, one active BuyAndHold strategy with a report,
    one shadow strategy, and one executed tick (orders, fills, snapshot)."""
    _seed_lake(settings.lake.path)
    with SqliteState(settings.state.path) as state:
        state.migrate()
        registry = StrategyRegistry(state=state, artifacts_dir=settings.registry.artifacts_dir)
        active_id = registry.register(
            BuyAndHold({"ticker": "UP.US", "allocation": 0.5}),
            reports=[SurvivalReport(test_id="oos", passed=True, metrics={"sharpe_oos": 1.5})],
            strategy_id="bah_active",
        )
        seed_status(registry, active_id, "active", default_book=True)
        shadow_id = registry.register(
            BuyAndHold({"ticker": "DOWN.US", "allocation": 1.0}),
            reports=[],
            strategy_id="bah_shadow",
        )
        lake = DuckDBLake(settings.lake.path)
        try:
            result = run_tick(
                state=state,
                lake=lake,
                registry=registry,
                settings=TickSettings(universe=["UP.US"], initial_cash=10_000.0),
                as_of=date(2026, 3, 20),
            )
        finally:
            lake.close()
    return {"active_id": active_id, "shadow_id": shadow_id, "tick_id": result.tick_id}


@pytest.fixture
def services(settings, seeded, fake_source):
    ctx = AppContext(settings, source_factory=lambda: fake_source)
    svc = Services.create(ctx)
    svc.start()
    yield svc
    svc.shutdown()

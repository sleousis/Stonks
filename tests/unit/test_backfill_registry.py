"""Backfill phases come from their package, in priority order; the source
seam's default company data and the plan settings the backfill needs."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, date, datetime

import pytest

from stonks.config import load_settings
from stonks.ingest.backfill.registry import all_phases, select_phases
from stonks.ingest.backfill.service import BackfillSetupError, check_plan
from stonks.ingest.ensure import vendor_limits
from stonks.ingest.metadata_bundle import MetadataBundle
from stonks.ingest.schemas import (
    DividendRow,
    FinancialStatementsBundle,
    NewsArticleRow,
    RawPriceBar,
    TickerProfile,
)
from stonks.ingest.sources.base import DataSource


def test_phases_run_in_the_documented_order():
    assert [p.phase_id for p in all_phases()] == [
        "symbols",
        "daily",
        "daily_topup",
        "actions",
        "fundamentals",
        "index_constituents",
        "exchange_details",
        "treasury",
        "calendar_extra",
        "id_mapping",
        "calendars",
        "macro",
        "fx",
        "news",
        "intraday_1h",
        "intraday_5m",
        "intraday_1m",
        "edgar",
        "borrow",
        "tvl",
    ]


def test_only_the_free_source_phases_are_unmetered():
    free = {p.phase_id for p in all_phases() if not p.metered}
    assert free == {"edgar", "borrow", "tvl"}


def test_select_phases_keeps_run_order_and_rejects_unknown_ids():
    assert [p.phase_id for p in select_phases(["news", "daily"])] == ["daily", "news"]
    with pytest.raises(ValueError, match="unknown backfill phases"):
        select_phases(["nope"])


def test_all_in_one_serves_full_history_bulk_and_intraday():
    limits = vendor_limits("eodhd", "all_in_one")
    assert (limits.max_history_days, limits.bulk, limits.intraday) == (None, True, True)


def test_the_shipped_plan_stays_free_and_env_names_the_paid_one(monkeypatch):
    assert load_settings().ensure.plans["eodhd"] == "free"
    monkeypatch.setenv("STONKS_EODHD_PLAN", "All_In_One")
    assert load_settings().ensure.plans["eodhd"] == "all_in_one"


def test_check_plan_refuses_the_free_plan():
    with pytest.raises(BackfillSetupError, match="STONKS_EODHD_PLAN"):
        check_plan("eodhd", {"eodhd": "free"})
    check_plan("eodhd", {"eodhd": "all_in_one"})


class _Source(DataSource):
    source_id = "fake"

    def list_tickers(self, exchange: str) -> list[str]:
        return []

    def fetch_prices(self, ticker: str, since=None, until=None) -> Iterable[RawPriceBar]:
        return []

    def fetch_fundamentals(self, ticker: str) -> FinancialStatementsBundle:
        return FinancialStatementsBundle()

    def fetch_metadata(self, ticker: str) -> MetadataBundle:
        return MetadataBundle(
            profile=TickerProfile(id=ticker, asset_class="equity"),
            dividends=(DividendRow(ticker=ticker, ex_date=date(2024, 1, 2), amount=0.5),),
            news=(
                NewsArticleRow(
                    ticker=ticker,
                    published_at=datetime(2024, 1, 2, tzinfo=UTC),
                    title="t",
                ),
            ),
        )


def test_the_default_company_data_keeps_only_the_parts_asked_for():
    data = _Source().fetch_company_data("A.US", ["dividends"])
    assert len(data.metadata.dividends) == 1
    assert data.metadata.news == ()
    assert data.metadata.profile is None
    assert data.statements is None
    with_profile = _Source().fetch_company_data("A.US", ["profile", "statements"])
    assert with_profile.metadata.profile is not None
    assert with_profile.metadata.dividends == ()
    assert with_profile.statements == FinancialStatementsBundle()

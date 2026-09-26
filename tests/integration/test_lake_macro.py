"""Integration tests for the macroeconomic-indicator surface: lake upsert
idempotency + pipeline orchestration. Hermetic — no network access."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from stonks.ingest.metadata_bundle import MetadataBundle
from stonks.ingest.pipeline import IngestPipeline
from stonks.ingest.schemas import (
    FinancialStatementsBundle,
    MacroIndicatorRow,
)
from stonks.ingest.sources.base import DataSource


def _frame(rows: list[MacroIndicatorRow]) -> pd.DataFrame:
    return pd.DataFrame([r.model_dump() for r in rows])


def test_macro_table_created_by_migration(lake):
    assert "macro_indicators" in lake.tables()


def test_upsert_macro_indicators_round_trips_rows(lake):
    rows = [
        MacroIndicatorRow(
            country_iso="USA",
            indicator="real_gdp_total",
            observation_date=date(2023, 1, 1),
            period="annual",
            country_name="United States",
            value=22996.10,
        ),
        MacroIndicatorRow(
            country_iso="USA",
            indicator="real_gdp_total",
            observation_date=date(2022, 1, 1),
            period="annual",
            value=22376.94,
        ),
    ]
    inserted = lake.upsert_macro_indicators(_frame(rows))
    assert inserted == 2

    df = lake.sql(
        "SELECT country_iso, indicator, observation_date, period, value "
        "FROM macro_indicators ORDER BY observation_date"
    )
    assert df.shape[0] == 2
    assert df["country_iso"].tolist() == ["USA", "USA"]
    assert df["indicator"].tolist() == ["real_gdp_total", "real_gdp_total"]
    assert df["value"].tolist() == [22376.94, 22996.10]


def test_upsert_macro_indicators_is_idempotent_on_pk(lake):
    """Re-running the same fetch must not duplicate rows; the (country_iso,
    indicator, observation_date) PK is the natural identity. A vendor
    revising a published value updates in place."""
    initial = MacroIndicatorRow(
        country_iso="USA",
        indicator="real_gdp_total",
        observation_date=date(2023, 1, 1),
        period="annual",
        value=22996.10,
    )
    revised = initial.model_copy(update={"value": 23000.00})

    lake.upsert_macro_indicators(_frame([initial]))
    lake.upsert_macro_indicators(_frame([initial]))  # exact dup
    lake.upsert_macro_indicators(_frame([revised]))  # vendor revision

    df = lake.sql("SELECT value FROM macro_indicators")
    assert df.shape[0] == 1
    assert df["value"].iloc[0] == 23000.00


def test_upsert_macro_indicators_keeps_null_value(lake):
    row = MacroIndicatorRow(
        country_iso="USA",
        indicator="real_gdp_total",
        observation_date=date(2024, 1, 1),
        value=None,
    )
    lake.upsert_macro_indicators(_frame([row]))
    df = lake.sql("SELECT value FROM macro_indicators")
    assert df.shape[0] == 1
    assert pd.isna(df["value"].iloc[0])


def test_upsert_macro_indicators_empty_frame_is_noop(lake):
    assert lake.upsert_macro_indicators(pd.DataFrame()) == 0


# ---- pipeline --------------------------------------------------------------


class _FakeMacroSource(DataSource):
    """Minimal :class:`DataSource` that replays canned macro rows. The other
    abstract methods raise: macro ingestion never touches them."""

    source_id = "fake-macro"

    def __init__(self, rows: list[MacroIndicatorRow]):
        self._rows = rows

    def list_tickers(self, exchange: str) -> list[str]:  # pragma: no cover - unused
        raise NotImplementedError

    def fetch_prices(self, ticker, since=None, until=None):  # pragma: no cover - unused
        del ticker, since, until
        return iter(())

    def fetch_fundamentals(self, ticker: str) -> FinancialStatementsBundle:  # pragma: no cover
        del ticker
        return FinancialStatementsBundle()

    def fetch_metadata(self, ticker: str) -> MetadataBundle:  # pragma: no cover
        del ticker
        return MetadataBundle()

    def fetch_macro_indicator(self, country_iso: str, indicator: str):
        return iter(
            r for r in self._rows if r.country_iso == country_iso and r.indicator == indicator
        )


def test_pipeline_run_macro_indicators_persists_and_records_run(lake):
    rows = [
        MacroIndicatorRow(
            country_iso="USA",
            indicator="real_gdp_total",
            observation_date=date(2023, 1, 1),
            period="annual",
            value=22996.10,
        ),
        MacroIndicatorRow(
            country_iso="DEU",
            indicator="real_gdp_total",
            observation_date=date(2023, 1, 1),
            period="annual",
            value=4072.20,
        ),
    ]
    pipeline = IngestPipeline(source=_FakeMacroSource(rows), lake=lake)

    result = pipeline.run_macro_indicators(
        countries=["USA", "DEU"],
        indicators=["real_gdp_total"],
    )

    assert result.status == "ok"
    assert result.tickers_ok == 2  # one (country, indicator) pair per "ticker"
    assert result.tickers_failed == 0

    df = lake.sql("SELECT country_iso, value FROM macro_indicators ORDER BY country_iso")
    assert df["country_iso"].tolist() == ["DEU", "USA"]


def test_pipeline_closes_ingest_run_even_when_loop_raises_baseexception(lake):
    """A long N×M run interrupted by SIGTERM (or SystemExit) must still
    close the `ingest_runs` row to a terminal status — leaving a `running`
    row behind orphans operator triage. Mirrors the same guard
    `run_fundamentals` carries for the equivalent equity flow."""

    class _CrashingSource(_FakeMacroSource):
        def fetch_macro_indicator(self, country_iso: str, indicator: str):
            raise SystemExit("simulated SIGTERM mid-loop")

    pipeline = IngestPipeline(source=_CrashingSource([]), lake=lake)

    with pytest.raises(SystemExit):
        pipeline.run_macro_indicators(countries=["USA"], indicators=["real_gdp_total"])

    df = lake.sql("SELECT status FROM ingest_runs WHERE kind = 'macro'")
    assert df.shape[0] == 1
    assert df["status"].iloc[0] != "running"


def test_pipeline_soft_fails_on_value_error_from_fetcher(lake):
    """`EodhdDataSource.fetch_macro_indicator` validates inputs and raises a
    domain error on bad ones. The pipeline must catch that as a per-pair
    soft-fail (not a hard crash) — otherwise a single bad pair aborts the
    whole multi-country run."""

    class _ValueErrorSource(_FakeMacroSource):
        def fetch_macro_indicator(self, country_iso: str, indicator: str):
            if country_iso == "BAD":
                from stonks.ingest.sources.base import DataSourceError

                raise DataSourceError(f"bad country_iso {country_iso!r}")
            return super().fetch_macro_indicator(country_iso, indicator)

    rows = [
        MacroIndicatorRow(
            country_iso="USA",
            indicator="real_gdp_total",
            observation_date=date(2023, 1, 1),
            value=1.0,
        ),
    ]
    pipeline = IngestPipeline(source=_ValueErrorSource(rows), lake=lake)
    result = pipeline.run_macro_indicators(countries=["USA", "BAD"], indicators=["real_gdp_total"])
    assert result.status == "partial"
    assert result.tickers_failed == 1
    assert result.tickers_ok == 1


def test_pipeline_run_macro_indicators_soft_fails_per_pair(lake):
    """A single (country, indicator) pair raising must not abort the run;
    it should be counted in tickers_failed and the rest still persisted."""

    class _PartiallyBrokenSource(_FakeMacroSource):
        def fetch_macro_indicator(self, country_iso: str, indicator: str):
            if country_iso == "ZZZ":
                from stonks.ingest.sources.base import DataSourceError

                raise DataSourceError("vendor blew up for ZZZ")
            return super().fetch_macro_indicator(country_iso, indicator)

    rows = [
        MacroIndicatorRow(
            country_iso="USA",
            indicator="real_gdp_total",
            observation_date=date(2023, 1, 1),
            period="annual",
            value=22996.10,
        ),
    ]
    pipeline = IngestPipeline(source=_PartiallyBrokenSource(rows), lake=lake)

    result = pipeline.run_macro_indicators(
        countries=["USA", "ZZZ"],
        indicators=["real_gdp_total"],
    )
    assert result.status == "partial"
    assert result.tickers_ok == 1
    assert result.tickers_failed == 1
    df = lake.sql("SELECT country_iso FROM macro_indicators")
    assert df["country_iso"].tolist() == ["USA"]

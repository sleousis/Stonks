"""DeFi TVL lake surface + pipeline orchestration. Hermetic — the source is
a stub that never touches the network."""

from __future__ import annotations

from datetime import date

import pandas as pd

from stonks.ingest.pipeline import IngestPipeline
from stonks.ingest.schemas import DefiTvlRow, FinancialStatementsBundle
from stonks.ingest.sources.base import DataSource, UnsupportedCapabilityError
from stonks.ingest.sources.defillama import DefiLlamaUnknownChainError


def _row(chain: str, day: date, tvl: float | None, source: str = "stub") -> DefiTvlRow:
    return DefiTvlRow(chain=chain, observation_date=day, tvl_usd=tvl, source=source)


def _frame(rows: list[DefiTvlRow]) -> pd.DataFrame:
    return pd.DataFrame([r.model_dump() for r in rows])


class _StubTvlSource(DataSource):
    source_id = "stub"

    def __init__(self, series: dict[str, list[DefiTvlRow]]):
        self._series = series
        self.calls: list[tuple[str, date | None]] = []

    def list_tickers(self, exchange):
        return []

    def fetch_prices(self, ticker, since=None, until=None):
        return []

    def fetch_fundamentals(self, ticker):
        return FinancialStatementsBundle()

    def fetch_chain_tvl(self, chain, since=None):
        self.calls.append((chain, since))
        if chain not in self._series:
            raise DefiLlamaUnknownChainError(chain)
        return [r for r in self._series[chain] if since is None or r.observation_date >= since]


# ---- lake ------------------------------------------------------------------


def test_defi_tvl_table_created_by_migration(lake):
    assert "defi_tvl" in lake.tables()


def test_upsert_and_read_round_trip(lake):
    rows = [
        _row("ethereum", date(2026, 1, 2), 2.0),
        _row("ethereum", date(2026, 1, 1), 1.0),
        _row("solana", date(2026, 1, 1), 9.0),
    ]
    assert lake.upsert_defi_tvl(_frame(rows)) == 3
    df = lake.get_defi_tvl("ethereum")
    assert list(df.columns) == ["observation_date", "tvl_usd", "source"]
    assert df["observation_date"].tolist() == [date(2026, 1, 1), date(2026, 1, 2)]
    assert df["tvl_usd"].tolist() == [1.0, 2.0]
    assert df["source"].tolist() == ["stub", "stub"]


def test_read_filters_by_date_window(lake):
    rows = [_row("ethereum", date(2026, 1, d), float(d)) for d in range(1, 6)]
    lake.upsert_defi_tvl(_frame(rows))
    df = lake.get_defi_tvl("ethereum", start=date(2026, 1, 2), end=date(2026, 1, 4))
    assert df["tvl_usd"].tolist() == [2.0, 3.0, 4.0]


def test_upsert_is_idempotent_and_last_write_wins(lake):
    lake.upsert_defi_tvl(_frame([_row("ethereum", date(2026, 1, 1), 1.0)]))
    lake.upsert_defi_tvl(_frame([_row("ethereum", date(2026, 1, 1), 1.0)]))
    lake.upsert_defi_tvl(_frame([_row("ethereum", date(2026, 1, 1), 1.5, source="other")]))
    df = lake.sql("SELECT chain, observation_date, tvl_usd, source FROM defi_tvl")
    assert len(df) == 1
    assert df["tvl_usd"].iloc[0] == 1.5
    assert df["source"].iloc[0] == "other"


def test_upsert_empty_frame_is_noop(lake):
    assert lake.upsert_defi_tvl(pd.DataFrame()) == 0


def test_read_unknown_chain_is_empty(lake):
    df = lake.get_defi_tvl("nochain")
    assert df.empty
    assert list(df.columns) == ["observation_date", "tvl_usd", "source"]


# ---- pipeline --------------------------------------------------------------


def _series() -> dict[str, list[DefiTvlRow]]:
    return {
        "ethereum": [_row("ethereum", date(2026, 1, d), float(d)) for d in range(1, 4)],
        "solana": [_row("solana", date(2026, 1, d), 10.0 * d) for d in range(1, 3)],
    }


def test_run_defi_tvl_upserts_and_records_ingest_run(lake):
    source = _StubTvlSource(_series())
    result = IngestPipeline(source=source, lake=lake).run_defi_tvl(["ethereum", "solana"])
    assert (result.kind, result.status, result.tickers_ok, result.tickers_failed) == (
        "defi_tvl",
        "ok",
        2,
        0,
    )
    assert len(lake.sql("SELECT * FROM defi_tvl")) == 5
    run = lake.sql(
        f"SELECT source, kind, status, tickers_ok FROM ingest_runs WHERE id = {result.run_id}"
    )
    assert run.iloc[0].tolist() == ["stub", "defi_tvl", "ok", 2]


def test_run_defi_tvl_is_idempotent(lake):
    pipeline = IngestPipeline(source=_StubTvlSource(_series()), lake=lake)
    pipeline.run_defi_tvl(["ethereum", "solana"])
    first = lake.sql("SELECT * FROM defi_tvl ORDER BY chain, observation_date")
    pipeline.run_defi_tvl(["ethereum", "solana"])
    second = lake.sql("SELECT * FROM defi_tvl ORDER BY chain, observation_date")
    pd.testing.assert_frame_equal(first, second)


def test_run_defi_tvl_passes_since(lake):
    source = _StubTvlSource(_series())
    IngestPipeline(source=source, lake=lake).run_defi_tvl(["ethereum"], since=date(2026, 1, 2))
    assert source.calls == [("ethereum", date(2026, 1, 2))]
    assert lake.get_defi_tvl("ethereum")["tvl_usd"].tolist() == [2.0, 3.0]


def test_run_defi_tvl_soft_fails_per_chain(lake):
    source = _StubTvlSource(_series())
    result = IngestPipeline(source=source, lake=lake).run_defi_tvl(["ethereum", "nochain"])
    assert (result.status, result.tickers_ok, result.tickers_failed) == ("partial", 1, 1)
    assert len(lake.get_defi_tvl("ethereum")) == 3


def test_run_defi_tvl_with_source_lacking_capability_soft_fails(lake):
    class _NoTvl(_StubTvlSource):
        def fetch_chain_tvl(self, chain, since=None):
            return DataSource.fetch_chain_tvl(self, chain, since)

    result = IngestPipeline(source=_NoTvl({}), lake=lake).run_defi_tvl(["ethereum"])
    assert (result.status, result.tickers_failed) == ("error", 1)
    assert issubclass(UnsupportedCapabilityError, Exception)

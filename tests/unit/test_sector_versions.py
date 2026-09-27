"""Point-in-time sector labels (roadmap 22.10): every change to an
instrument's sector is kept with the time Stonks first saw it (P12)."""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import pytest

from stonks.store.lake import DuckDBLake

T1 = datetime(2024, 1, 5, 12)
T2 = datetime(2024, 6, 3, 9)


@pytest.fixture
def lake(tmp_path):
    db = DuckDBLake(tmp_path / "lake.duckdb")
    db.migrate()
    yield db
    db.close()


def _profile(ticker: str, sector: str | None, gic: str | None = None) -> pd.DataFrame:
    return pd.DataFrame(
        [{"id": ticker, "asset_class": "equity", "sector": sector, "gic_sector": gic}]
    )


def test_a_change_of_sector_adds_a_version(lake):
    lake.upsert_instrument_profile(_profile("A.US", "Tech", "IT"), known_at=T1)
    lake.upsert_instrument_profile(_profile("A.US", "Tech", "IT"), known_at=T1.replace(day=9))
    lake.upsert_instrument_profile(_profile("A.US", None), known_at=T2.replace(day=1))
    lake.upsert_instrument_profile(_profile("A.US", "Energy", "IT"), known_at=T2)
    got = lake.instrument_sector_versions(["A.US", "B.US"])
    assert list(got.columns) == ["id", "sector", "gic_sector", "known_at"]
    assert got["sector"].tolist() == ["Tech", "Energy"]
    assert [pd.Timestamp(t).to_pydatetime() for t in got["known_at"]] == [T1, T2]
    assert lake.instrument_sectors(["A.US"])["sector"].tolist() == ["Energy"]


def test_a_profile_without_a_sector_adds_no_version(lake):
    lake.upsert_instrument_profile(_profile("C.US", None), known_at=T1)
    assert lake.instrument_sector_versions(["C.US"]).empty

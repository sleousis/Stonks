"""The insider net buying factor (roadmap 23.13): open-market trades only,
each counted from the acceptance time of its Form 4 (P12)."""

from __future__ import annotations

from datetime import date, datetime

import pandas as pd
import pytest

from stonks.factors.registry import factor_sets, get_factor
from stonks.store.lake import DuckDBLake


def _trade(ticker, day, code, value, known=None, filed=None, owner="A"):
    return {
        "ticker": ticker,
        "transaction_date": day,
        "filing_date": filed or day,
        "owner_name": owner,
        "owner_cik": None,
        "owner_relation": "officer",
        "owner_title": None,
        "transaction_code": code,
        "acquired_disposed": "A" if code == "P" else "D",
        "post_transaction_amount": None,
        "shares": 1.0,
        "price": value,
        "value": value,
        "sec_link": f"{ticker}{day}{code}{value}",
        "known_at": known,
    }


@pytest.fixture
def lake(tmp_path):
    db = DuckDBLake(tmp_path / "lake.duckdb")
    db.migrate()
    rows = [
        _trade("A.US", date(2026, 3, 2), "P", 300.0, known=datetime(2026, 3, 4, 22)),
        _trade("A.US", date(2026, 3, 3), "S", 100.0, known=datetime(2026, 3, 5, 22)),
        _trade("A.US", date(2026, 3, 3), "M", 999.0, known=datetime(2026, 3, 5, 22)),
        _trade("B.US", date(2026, 3, 2), "S", 50.0, filed=date(2026, 3, 4)),
        _trade("C.US", date(2025, 1, 2), "P", 50.0, known=datetime(2025, 1, 3)),
    ]
    frame = pd.DataFrame(rows)
    frame["known_at"] = pd.to_datetime(frame["known_at"])
    db.upsert_insider_transactions(frame)
    yield db
    db.close()


def _values(lake, day):
    return get_factor("insider_net_buying").values_at(lake, ["A.US", "B.US", "C.US"], day)


def test_net_buying_ratio_counts_open_market_trades(lake):
    got = _values(lake, datetime(2026, 3, 10))
    assert got == pytest.approx({"A.US": 0.5, "B.US": -1.0})  # C.US is too old


def test_trades_count_from_their_acceptance(lake):
    # on 03-04 the purchase's Form 4 lands at 22:00 UTC, before the day's end
    assert _values(lake, datetime(2026, 3, 4)) == pytest.approx({"A.US": 1.0})
    assert _values(lake, datetime(2026, 3, 3)) == {}
    # the vendor row without an acceptance time counts from the day after filing
    assert "B.US" in _values(lake, datetime(2026, 3, 5))


def test_registered_with_its_paper():
    assert factor_sets()["filings"] == ["insider_net_buying"]
    f = get_factor("insider_net_buying")
    assert f.provenance is not None and f.provenance.published == 2001
    assert f.tables == ("insider_transactions",)

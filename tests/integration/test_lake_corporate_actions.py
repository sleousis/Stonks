"""Reading splits and dividends from the lake as corporate actions."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from stonks.core.corporate_actions import Dividend, Split
from stonks.store.corporate_actions import LakeCorporateActions
from stonks.store.lake import DuckDBLake


def _dividends(rows: list[dict]) -> pd.DataFrame:
    extra = {"currency": None, "pay_date": None, "record_date": None, "declaration_date": None}
    return pd.DataFrame([{**extra, **r} for r in rows])


@pytest.fixture
def lake(tmp_path):
    lake = DuckDBLake(tmp_path / "lake.duckdb")
    lake.migrate()
    lake.upsert_stock_splits(
        pd.DataFrame(
            [
                {"ticker": "A.US", "date": date(2024, 6, 10), "ratio": 10.0},
                {"ticker": "B.US", "date": date(2024, 3, 1), "ratio": 0.5},
            ]
        )
    )
    lake.upsert_dividends(
        _dividends(
            [
                {"ticker": "A.US", "ex_date": date(2024, 6, 10), "amount": 0.25},
                {"ticker": "A.US", "ex_date": date(2024, 2, 9), "amount": 2.0},
                {"ticker": "C.US", "ex_date": date(2024, 2, 9), "amount": 1.0},
            ]
        )
    )
    yield lake
    lake.close()


def test_get_corporate_actions_returns_both_kinds_for_requested_tickers(lake):
    df = lake.get_corporate_actions(["A.US", "B.US"])
    assert list(df.columns) == ["ticker", "ex_date", "kind", "value"]
    rows = [(r.ticker, r.ex_date, r.kind, r.value) for r in df.itertuples(index=False)]
    assert rows == [
        ("A.US", date(2024, 2, 9), "dividend", 2.0),
        ("A.US", date(2024, 6, 10), "split", 10.0),
        ("A.US", date(2024, 6, 10), "dividend", 0.25),
        ("B.US", date(2024, 3, 1), "split", 0.5),
    ]


def test_get_corporate_actions_empty_universe(lake):
    assert lake.get_corporate_actions([]).empty


def test_provider_loads_typed_events_in_one_query(lake):
    calls = []

    class Spy:
        def __getattr__(self, name):
            return getattr(lake, name)

        def get_corporate_actions(self, tickers):
            calls.append(list(tickers))
            return lake.get_corporate_actions(tickers)

    actions = LakeCorporateActions(Spy()).load(["A.US", "B.US", "C.US", "D.US"])
    assert calls == [["A.US", "B.US", "C.US", "D.US"]]
    assert actions.for_ticker("A.US") == (
        Dividend("A.US", date(2024, 2, 9), 2.0),
        Split("A.US", date(2024, 6, 10), 10.0),
        Dividend("A.US", date(2024, 6, 10), 0.25),
    )
    assert actions.for_ticker("B.US") == (Split("B.US", date(2024, 3, 1), 0.5),)
    assert actions.for_ticker("D.US") == ()


def test_provider_tolerates_lake_without_corporate_action_reader():
    class BarsOnly:
        def get_bars(self, *a, **k):
            return pd.DataFrame()

    assert not LakeCorporateActions(BarsOnly()).load(["A.US"])

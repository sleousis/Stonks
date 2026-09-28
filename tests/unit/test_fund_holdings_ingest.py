"""Fund holdings (roadmap 23.14): the row schema, the EODHD ETF parser, the
DataSource default, the lake store read point in time, and the pipeline."""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from stonks.ingest.pipeline import IngestPipeline
from stonks.ingest.schemas import FinancialStatementsBundle, FundHoldingRow, RawPriceBar
from stonks.ingest.sources.base import DataSource, UnsupportedCapabilityError
from stonks.ingest.sources.eodhd import EodhdDataSource, parse_fund_holdings_from_fundamentals
from stonks.store.lake import DuckDBLake

FIXTURES = Path(__file__).parent.parent / "fixtures" / "eodhd"


def _payload() -> Any:
    return json.loads((FIXTURES / "spy_etf_fundamentals.json").read_text())


# ---- schema ----------------------------------------------------------------------


def test_row_takes_a_weight_as_a_fraction() -> None:
    row = FundHoldingRow(
        fund="SPY.US", holding="AAPL.US", as_of=date(2026, 9, 25), weight=0.07, source="fake"
    )
    assert row.weight == pytest.approx(0.07)
    with pytest.raises(ValidationError):
        FundHoldingRow(fund="SPY.US", holding="X", as_of=date(2026, 9, 25), weight=-0.1,
                       source="fake")  # fmt: skip
    with pytest.raises(ValidationError):
        FundHoldingRow(fund="SPY.US", holding="X", as_of=date(2026, 9, 25), weight=0.1,
                       source="fake", country="United States")  # fmt: skip


# ---- EODHD parser ----------------------------------------------------------------


def test_eodhd_parser_maps_holdings_to_domain_rows() -> None:
    rows = parse_fund_holdings_from_fundamentals("SPY.US", _payload(), today=date(2026, 9, 28))
    by = {r.holding: r for r in rows}
    assert set(by) == {"AAPL.US", "MSFT.US", "ASML.AS", "XYZ"}
    aapl = by["AAPL.US"]
    assert aapl.fund == "SPY.US"
    assert aapl.weight == pytest.approx(0.071)
    assert aapl.as_of == date(2026, 9, 25)
    assert aapl.name == "Apple Inc"
    assert aapl.sector == "Technology"
    assert aapl.country == "US"
    assert aapl.source == "eodhd"
    assert by["MSFT.US"].weight == pytest.approx(0.065)
    assert by["ASML.AS"].country == "NL"
    assert by["XYZ"].country is None  # unknown country name is dropped, not stored raw


def test_eodhd_parser_never_dates_holdings_after_today() -> None:
    payload = _payload()
    payload["General"]["UpdatedAt"] = "2027-01-01"
    rows = parse_fund_holdings_from_fundamentals("SPY.US", payload, today=date(2026, 9, 28))
    assert {r.as_of for r in rows} == {date(2026, 9, 28)}
    del payload["General"]["UpdatedAt"]
    rows = parse_fund_holdings_from_fundamentals("SPY.US", payload, today=date(2026, 9, 28))
    assert {r.as_of for r in rows} == {date(2026, 9, 28)}


def test_eodhd_parser_returns_nothing_for_a_stock() -> None:
    payload = {"General": {"Code": "AAPL", "Type": "Common Stock"}}
    assert parse_fund_holdings_from_fundamentals("AAPL.US", payload) == []
    assert parse_fund_holdings_from_fundamentals("AAPL.US", "text error") == []


class _Session:
    def __init__(self, payload: Any) -> None:
        self.payload = payload
        self.urls: list[str] = []

    def get(self, url: str, params: Any = None, timeout: Any = None) -> Any:
        self.urls.append(url)
        payload = self.payload

        class _Resp:
            status_code = 200
            text = json.dumps(payload)

            def raise_for_status(self) -> None:
                return None

            def json(self) -> Any:
                return payload

        return _Resp()


def test_eodhd_source_reads_the_fundamentals_endpoint() -> None:
    session = _Session(_payload())
    src = EodhdDataSource("key", session=session)  # type: ignore[arg-type]
    rows = list(src.fetch_fund_holdings("SPY.US"))
    assert len(rows) == 4
    assert session.urls[0].endswith("/fundamentals/SPY.US")


def test_data_source_default_is_unsupported() -> None:
    class _Bare(DataSource):
        source_id = "bare"

        def list_tickers(self, exchange: str) -> list[str]:
            return []

        def fetch_prices(
            self, ticker: str, since: date | None = None, until: date | None = None
        ) -> Iterable[RawPriceBar]:
            return []

        def fetch_fundamentals(self, ticker: str) -> FinancialStatementsBundle:
            return FinancialStatementsBundle()

    with pytest.raises(UnsupportedCapabilityError):
        _Bare().fetch_fund_holdings("SPY.US")


# ---- lake ------------------------------------------------------------------------


def _row(fund: str, holding: str, day: date, weight: float, **kw: Any) -> FundHoldingRow:
    return FundHoldingRow(fund=fund, holding=holding, as_of=day, weight=weight,
                          source=kw.pop("source", "fake"), **kw)  # fmt: skip


def _frame(rows: list[FundHoldingRow]) -> Any:
    import pandas as pd

    return pd.DataFrame([r.model_dump() for r in rows])


def test_lake_reads_the_latest_snapshot_known_by_the_day(lake: DuckDBLake) -> None:
    lake.upsert_fund_holdings(
        _frame([_row("SPY.US", "AAPL.US", date(2026, 6, 30), 0.06, sector="Technology")]),
        known_at=datetime(2026, 7, 2),
    )
    lake.upsert_fund_holdings(
        _frame(
            [
                _row("SPY.US", "AAPL.US", date(2026, 9, 25), 0.07, sector="Technology"),
                _row("SPY.US", "MSFT.US", date(2026, 9, 25), 0.065),
            ]
        ),
        known_at=datetime(2026, 9, 28),
    )
    now = lake.fund_holdings(["SPY.US"], as_of=date(2026, 9, 28))
    assert sorted(now["holding"]) == ["AAPL.US", "MSFT.US"]
    # Before Stonks saw the September file, the June one is what was known.
    past = lake.fund_holdings(["SPY.US"], as_of=date(2026, 9, 26))
    assert list(past["holding"]) == ["AAPL.US"]
    assert past["weight"].iloc[0] == pytest.approx(0.06)
    # Nothing known yet.
    assert lake.fund_holdings(["SPY.US"], as_of=date(2026, 7, 1)).empty
    assert lake.fund_holdings([], as_of=date(2026, 9, 28)).empty
    assert lake.fund_holdings(["QQQ.US"], as_of=date(2026, 9, 28)).empty


def test_lake_rerun_keeps_the_first_known_at(lake: DuckDBLake) -> None:
    frame = _frame([_row("SPY.US", "AAPL.US", date(2026, 9, 25), 0.07)])
    lake.upsert_fund_holdings(frame, known_at=datetime(2026, 9, 26))
    lake.upsert_fund_holdings(
        _frame([_row("SPY.US", "AAPL.US", date(2026, 9, 25), 0.071)]),
        known_at=datetime(2026, 9, 28),
    )
    got = lake.fund_holdings(["SPY.US"], as_of=date(2026, 9, 26))
    assert got["weight"].iloc[0] == pytest.approx(0.071)
    assert lake.funds_with_holdings() == ["SPY.US"]


def test_lake_keeps_one_source_per_fund(lake: DuckDBLake) -> None:
    """Two vendors for one fund on one day: the reader takes one source, so
    weights never double count."""
    lake.upsert_fund_holdings(
        _frame(
            [
                _row("SPY.US", "AAPL.US", date(2026, 9, 25), 0.07, source="a"),
                _row("SPY.US", "AAPL.US", date(2026, 9, 25), 0.069, source="b"),
            ]
        ),
        known_at=datetime(2026, 9, 26),
    )
    got = lake.fund_holdings(["SPY.US"], as_of=date(2026, 9, 28))
    assert len(got) == 1
    assert got["source"].iloc[0] == "a"


# ---- pipeline --------------------------------------------------------------------


class _FundSource(DataSource):
    source_id = "fake"

    def __init__(self, holdings: dict[str, list[FundHoldingRow]]) -> None:
        self._holdings = holdings

    def list_tickers(self, exchange: str) -> list[str]:
        return []

    def fetch_prices(
        self, ticker: str, since: date | None = None, until: date | None = None
    ) -> Iterable[RawPriceBar]:
        return []

    def fetch_fundamentals(self, ticker: str) -> FinancialStatementsBundle:
        return FinancialStatementsBundle()

    def fetch_fund_holdings(self, fund: str) -> Iterable[FundHoldingRow]:
        if fund not in self._holdings:
            raise UnsupportedCapabilityError(f"no holdings for {fund}")
        return self._holdings[fund]


def test_pipeline_ingests_each_fund_as_one_unit(lake: DuckDBLake) -> None:
    source = _FundSource({"SPY.US": [_row("SPY.US", "AAPL.US", date(2026, 9, 25), 0.07)]})
    result = IngestPipeline(source, lake).run_fund_holdings(["SPY.US", "NOPE.US"])
    assert (result.kind, result.tickers_ok, result.tickers_failed) == ("funds", 1, 1)
    assert result.failed == ("NOPE.US",)
    assert lake.funds_with_holdings() == ["SPY.US"]

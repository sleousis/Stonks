"""FX ingest (roadmap 20.5): EODHD forex parsing and request shape, the
optional DataSource capability, pair parsing and the pipeline run."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from typing import Any

import pytest

from stonks.ingest.fx import parse_pair, parse_pairs
from stonks.ingest.pipeline import IngestPipeline
from stonks.ingest.schemas import FinancialStatementsBundle, FxRateRow, RawPriceBar
from stonks.ingest.sources.base import DataSource, UnsupportedCapabilityError
from stonks.ingest.sources.eodhd import EodhdDataSource, EodhdFreeTierError, parse_fx_response
from stonks.store.lake import DuckDBLake


class _Response:
    def __init__(self, body: Any) -> None:
        self.status_code = 200
        self._body = body
        self.text = ""

    def json(self) -> Any:
        return self._body

    def raise_for_status(self) -> None:
        return None


class _Session:
    def __init__(self, body: Any) -> None:
        self.body = body
        self.calls: list[tuple[str, dict[str, str]]] = []

    def get(self, url: str, params: dict[str, str], timeout: int) -> _Response:
        self.calls.append((url, params))
        return _Response(self.body)


def test_parse_fx_response_keeps_positive_closes():
    rows = parse_fx_response(
        "EUR",
        "USD",
        [
            {"date": "2026-01-05", "close": 1.1},
            {"date": "2026-01-06", "close": 0},
            {"date": "2026-01-07", "close": None},
            {"close": 1.2},
            {"date": "2026-01-08", "close": "bad"},
            "junk",
        ],
    )
    assert rows == [
        FxRateRow(
            base_currency="EUR",
            quote_currency="USD",
            observation_date=date(2026, 1, 5),
            rate=1.1,
            source="eodhd",
        )
    ]
    assert parse_fx_response("EUR", "USD", {"not": "a list"}) == []


def test_parse_fx_response_free_tier_text_raises():
    with pytest.raises(EodhdFreeTierError):
        parse_fx_response("EUR", "USD", "Only EOD data allowed for free users")


def test_eodhd_fetch_fx_rates_url_and_params():
    session = _Session([{"date": "2026-01-05", "close": 1.1}])
    src = EodhdDataSource(api_key="k", session=session)  # type: ignore[arg-type]
    rows = list(src.fetch_fx_rates("eur", "usd", since=date(2026, 1, 1), until=date(2026, 1, 9)))
    url, params = session.calls[0]
    assert url.endswith("/eod/EURUSD.FOREX")
    assert params["from"] == "2026-01-01" and params["to"] == "2026-01-09"
    assert params["fmt"] == "json"
    assert rows[0].base_currency == "EUR" and rows[0].rate == 1.1


class _FakeFx(DataSource):
    source_id = "fake"

    def __init__(self, rates: dict[tuple[str, str], list[tuple[date, float]]]) -> None:
        self.rates = rates

    def list_tickers(self, exchange: str) -> list[str]:
        return []

    def fetch_prices(
        self, ticker: str, since: date | None = None, until: date | None = None
    ) -> Iterable[RawPriceBar]:
        return []

    def fetch_fundamentals(self, ticker: str) -> FinancialStatementsBundle:
        return FinancialStatementsBundle()

    def fetch_fx_rates(
        self, base: str, quote: str, since: date | None = None, until: date | None = None
    ) -> Iterable[FxRateRow]:
        if (base, quote) not in self.rates:
            raise UnsupportedCapabilityError(f"no pair {base}{quote}")
        return [
            FxRateRow(
                base_currency=base, quote_currency=quote, observation_date=d, rate=r, source="fake"
            )
            for d, r in self.rates[(base, quote)]
        ]


def test_default_capability_is_unsupported():
    class Bare(_FakeFx):
        fetch_fx_rates = DataSource.fetch_fx_rates  # type: ignore[assignment]

    with pytest.raises(UnsupportedCapabilityError, match="FX rates"):
        list(Bare({}).fetch_fx_rates("EUR", "USD"))


def test_pipeline_run_fx_rates_soft_fails_per_pair(tmp_path):
    lake = DuckDBLake(tmp_path / "lake.duckdb")
    lake.migrate()
    source = _FakeFx({("EUR", "USD"): [(date(2026, 1, 5), 1.1), (date(2026, 1, 6), 1.2)]})
    result = IngestPipeline(source, lake).run_fx_rates([("EUR", "USD"), ("CHF", "USD")])
    assert (result.kind, result.status, result.tickers_ok, result.tickers_failed) == (
        "fx",
        "partial",
        1,
        1,
    )
    assert len(lake.get_fx_rates()) == 2
    again = IngestPipeline(source, lake).run_fx_rates([("EUR", "USD")])
    assert again.status == "ok" and len(lake.get_fx_rates()) == 2  # idempotent
    lake.close()


def test_parse_pairs():
    assert parse_pair("eur/usd") == ("EUR", "USD")
    assert parse_pairs("EURUSD, GBP-USD,EURUSD") == [("EUR", "USD"), ("GBP", "USD")]
    for bad in ("EURO", "USDUSD", "EU/US"):
        with pytest.raises(ValueError):
            parse_pair(bad)

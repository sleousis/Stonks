"""Unit tests for the EODHD source: pure response parsers (via fixtures) and
the HTTP 403 → EodhdFreeTierError mapping (via a fake requests.Session)."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from stonks.ingest.schemas import FundamentalRow, RawPriceBar
from stonks.ingest.sources.eodhd import (
    EodhdDataSource,
    EodhdFreeTierError,
    parse_fundamentals_response,
    parse_prices_response,
)

FIXTURES = Path(__file__).parent.parent / "fixtures" / "eodhd"


def _load(name: str):
    return json.loads((FIXTURES / name).read_text())


def test_parse_prices_basic_shape():
    bars = list(parse_prices_response("AAPL.US", _load("aapl_prices.json")))
    assert len(bars) == 10
    first = bars[0]
    assert isinstance(first, RawPriceBar)
    assert first.ticker == "AAPL.US"
    assert first.date == date(2026, 4, 1)
    assert first.close == 255.63
    assert first.adj_close == 255.63    # mapped from "adjusted_close"
    assert first.volume == 40_059_400


def test_parse_prices_skips_missing_fields_gracefully():
    bars = list(parse_prices_response("X.US", []))
    assert bars == []


def test_parse_prices_raises_on_free_tier_warning_shape():
    payload = [{"warning": "Data is limited by one year as you have free subscription"}]
    with pytest.raises(EodhdFreeTierError):
        list(parse_prices_response("AAPL.US", payload))


def test_parse_fundamentals_yields_rows_for_all_statements_and_frequencies():
    rows = list(parse_fundamentals_response("AAPL.US", _load("aapl_fundamentals.json")))
    assert all(isinstance(r, FundamentalRow) for r in rows)
    assert all(r.ticker == "AAPL.US" for r in rows)

    statements = {r.statement for r in rows}
    assert statements == {"income", "balance", "cashflow"}

    frequencies = {r.frequency for r in rows}
    assert frequencies == {"Q", "A"}


def test_parse_fundamentals_coerces_string_numbers_to_floats():
    rows = list(parse_fundamentals_response("AAPL.US", _load("aapl_fundamentals.json")))
    rev = next(r for r in rows if r.line_item == "totalRevenue"
               and r.frequency == "Q" and r.period_end == date(2025, 12, 31))
    assert rev.value == 124_300_000_000.0
    assert isinstance(rev.value, float)


def test_parse_fundamentals_skips_date_field_as_line_item():
    rows = list(parse_fundamentals_response("AAPL.US", _load("aapl_fundamentals.json")))
    assert not any(r.line_item == "date" for r in rows)


def test_parse_fundamentals_raises_on_free_tier_error_text():
    with pytest.raises(EodhdFreeTierError):
        list(parse_fundamentals_response(
            "AAPL.US",
            "Only EOD data allowed for free users. Please, contact our support team: support@eodhistoricaldata.com",
        ))


# ---- HTTP layer ------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code: int, body: Any):
        self.status_code = status_code
        self._body = body

    @property
    def text(self) -> str:
        return self._body if isinstance(self._body, str) else json.dumps(self._body)

    def json(self) -> Any:
        if isinstance(self._body, str):
            try:
                return json.loads(self._body)
            except ValueError:
                raise
        return self._body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            from requests.exceptions import HTTPError

            raise HTTPError(f"{self.status_code} error", response=self)


class _FakeSession:
    def __init__(self, response: _FakeResponse):
        self.response = response
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params or {}))
        return self.response


def test_fetch_fundamentals_maps_403_to_free_tier_error():
    session = _FakeSession(
        _FakeResponse(status_code=403, body="Only EOD data allowed for free users.")
    )
    source = EodhdDataSource(api_key="k", max_retries=1, session=session)  # type: ignore[arg-type]
    with pytest.raises(EodhdFreeTierError):
        list(source.fetch_fundamentals("AAPL.US"))
    assert len(session.calls) == 1  # no retries on free-tier signal


def test_fetch_prices_parses_happy_json_response():
    payload = json.loads((FIXTURES / "aapl_prices.json").read_text())
    session = _FakeSession(_FakeResponse(status_code=200, body=payload))
    source = EodhdDataSource(api_key="k", session=session)  # type: ignore[arg-type]

    bars = list(source.fetch_prices("AAPL.US", since=date(2026, 4, 1), until=date(2026, 4, 15)))
    assert len(bars) == 10
    url, params = session.calls[0]
    assert url.endswith("/eod/AAPL.US")
    assert params["from"] == "2026-04-01"
    assert params["to"] == "2026-04-15"
    assert params["api_token"] == "k"

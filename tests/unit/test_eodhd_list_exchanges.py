"""Unit test for EodhdDataSource.list_exchanges and its pure parser.

Covers:
  * field mapping (vendor PascalCase -> canonical snake_case)
  * single-call HTTP shape (one GET to /exchanges-list with fmt=json)
  * defensive handling of non-list payloads
  * pass-through of virtual asset-class buckets (FOREX/CC/...)
"""

from __future__ import annotations

import json
from typing import Any

from stonks.ingest.schemas import ExchangeInfo
from stonks.ingest.sources.eodhd import EodhdDataSource, parse_exchanges_response


class _Response:
    def __init__(self, status: int, body: Any):
        self.status_code = status
        self._body = body

    @property
    def text(self) -> str:
        return self._body if isinstance(self._body, str) else json.dumps(self._body)

    def json(self) -> Any:
        return self._body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            from requests.exceptions import HTTPError

            raise HTTPError(f"{self.status_code} error", response=self)


class _RecordingSession:
    """Fake session that returns a single canned response for any GET and
    records the calls so tests can assert on URL + params."""

    def __init__(self, body: Any):
        self._body = body
        self.calls: list[tuple[str, dict[str, str]]] = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        return _Response(200, self._body)


_SAMPLE_PAYLOAD = [
    {
        "Name": "USA Stocks",
        "Code": "US",
        "OperatingMIC": "XNAS, XNYS",
        "Country": "USA",
        "Currency": "USD",
        "CountryISO2": "US",
        "CountryISO3": "USA",
    },
    {
        "Name": "London Exchange",
        "Code": "LSE",
        "OperatingMIC": "XLON",
        "Country": "UK",
        "Currency": "GBP",
        "CountryISO2": "GB",
        "CountryISO3": "GBR",
    },
    {
        "Name": "FOREX",
        "Code": "FOREX",
        "OperatingMIC": None,
        "Country": "Unknown",
        "Currency": None,
        "CountryISO2": None,
        "CountryISO3": None,
    },
]


def test_parse_exchanges_response_maps_vendor_fields():
    rows = list(parse_exchanges_response(_SAMPLE_PAYLOAD))

    assert len(rows) == 3
    us = rows[0]
    assert isinstance(us, ExchangeInfo)
    assert us.code == "US"
    assert us.name == "USA Stocks"
    assert us.country == "USA"
    assert us.currency == "USD"
    assert us.country_iso2 == "US"
    assert us.country_iso3 == "USA"
    assert us.operating_mic == "XNAS, XNYS"


def test_parse_exchanges_response_skips_rows_without_code():
    payload = [
        {"Code": "US", "Name": "USA Stocks"},
        {"Name": "Missing-Code"},
        "not-a-dict",
        {"Code": "LSE"},
    ]
    rows = list(parse_exchanges_response(payload))
    assert [r.code for r in rows] == ["US", "LSE"]


def test_list_exchanges_issues_single_call_and_returns_rows():
    session = _RecordingSession(_SAMPLE_PAYLOAD)
    source = EodhdDataSource(api_key="k", session=session)  # type: ignore[arg-type]

    rows = list(source.list_exchanges())

    assert len(session.calls) == 1
    url, params = session.calls[0]
    assert url.endswith("/exchanges-list")
    assert params.get("fmt") == "json"
    assert params.get("api_token") == "k"
    assert [r.code for r in rows] == ["US", "LSE", "FOREX"]


def test_list_exchanges_tolerates_non_list_payload():
    """Empty dict / unexpected payload yields an empty iterable rather than
    raising — same shape of defensiveness used elsewhere in the parsers."""
    session = _RecordingSession({})
    source = EodhdDataSource(api_key="k", session=session)  # type: ignore[arg-type]
    assert list(source.list_exchanges()) == []


def test_list_exchanges_passes_through_virtual_buckets():
    """Per design: do not filter virtual asset-class 'exchanges'. The caller
    decides whether to drop FOREX/CC/INDX/etc."""
    session = _RecordingSession(_SAMPLE_PAYLOAD)
    source = EodhdDataSource(api_key="k", session=session)  # type: ignore[arg-type]
    codes = [r.code for r in source.list_exchanges()]
    assert "FOREX" in codes

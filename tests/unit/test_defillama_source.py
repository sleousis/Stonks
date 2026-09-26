"""DefiLlama adapter: payload parsing, vendor -> domain mapping, HTTP
hardening. Hermetic — every request goes through a stub session."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import requests

from stonks.ingest.schemas import DefiTvlRow
from stonks.ingest.sources.base import DataSource, DataSourceError, UnsupportedCapabilityError
from stonks.ingest.sources.defillama import (
    DefiLlamaDataSource,
    DefiLlamaResponseError,
    DefiLlamaUnknownChainError,
    normalize_chain,
    parse_chain_tvl_response,
)

FIXTURE = (
    Path(__file__).parent.parent / "fixtures" / "defillama" / "ethereum_historical_chain_tvl.json"
)


def _payload() -> list[dict[str, Any]]:
    return json.loads(FIXTURE.read_text())


class _StubResponse:
    def __init__(self, *, status_code: int = 200, json_body: Any = None, text: str = ""):
        self.status_code = status_code
        self._json = json_body
        self.text = text if json_body is None else json.dumps(json_body)

    def json(self) -> Any:
        if self._json is None:
            raise ValueError("not json")
        return self._json

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            err = requests.HTTPError(f"HTTP {self.status_code}")
            err.response = self  # type: ignore[assignment]
            raise err


class _StubSession:
    def __init__(self, *responses: Any):
        self._responses = list(responses)
        self.calls: list[tuple[str, float | None]] = []

    def get(self, url: str, params: Any = None, timeout: float | None = None) -> Any:
        self.calls.append((url, timeout))
        item = self._responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def close(self) -> None:
        pass


def _source(session: _StubSession, **kw: Any) -> DefiLlamaDataSource:
    return DefiLlamaDataSource(session=session, retry_backoff_seconds=0.0, **kw)  # type: ignore[arg-type]


# ---- parsing ---------------------------------------------------------------


def test_parse_recorded_fixture_maps_vendor_fields_to_domain_columns():
    rows = list(parse_chain_tvl_response("ethereum", _payload(), source="defillama"))
    assert len(rows) == 10
    assert all(isinstance(r, DefiTvlRow) for r in rows)
    first = rows[0]
    assert first.chain == "ethereum"
    assert first.observation_date == date(2026, 9, 17)  # unix 00:00 UTC -> calendar date
    assert first.tvl_usd == pytest.approx(49196408868.0)
    assert first.source == "defillama"
    assert [r.observation_date for r in rows] == sorted(r.observation_date for r in rows)


def test_parse_drops_malformed_rows_and_dedups_dates():
    payload = [
        {"date": 1790208000, "tvl": 1.0},
        {"date": "garbage", "tvl": 2.0},
        {"tvl": 3.0},
        {"date": 1790294400, "tvl": None},
        {"date": 1790294400, "tvl": 5.0},  # same day again: last wins
        {"date": 1790380800, "tvl": -1.0},  # negative TVL is not a valid observation
        "not a dict",
    ]
    rows = list(parse_chain_tvl_response("ethereum", payload, source="defillama"))
    assert [(r.observation_date, r.tvl_usd) for r in rows] == [
        (date(2026, 9, 24), 1.0),
        (date(2026, 9, 25), 5.0),
    ]


def test_parse_non_list_payload_is_a_soft_fail_error():
    with pytest.raises(DefiLlamaResponseError):
        list(parse_chain_tvl_response("ethereum", {"message": "oops"}, source="defillama"))
    assert issubclass(DefiLlamaResponseError, DataSourceError)


@pytest.mark.parametrize(
    ("raw", "canonical"),
    [
        ("Ethereum", "ethereum"),
        ("  SOLANA ", "solana"),
        ("BSC", "bsc"),
        ("Arbitrum Nova", "arbitrum nova"),
    ],
)
def test_normalize_chain(raw, canonical):
    assert normalize_chain(raw) == canonical


def test_normalize_chain_rejects_empty():
    with pytest.raises(ValueError):
        normalize_chain("   ")


# ---- fetch -----------------------------------------------------------------


def test_fetch_chain_tvl_hits_historical_endpoint_with_timeout():
    session = _StubSession(_StubResponse(json_body=_payload()))
    src = _source(session, timeout_seconds=7)
    rows = list(src.fetch_chain_tvl("Ethereum"))
    assert len(rows) == 10
    assert rows[0].chain == "ethereum"
    url, timeout = session.calls[0]
    assert url == "https://api.llama.fi/v2/historicalChainTvl/ethereum"
    assert timeout == 7


def test_fetch_chain_tvl_url_encodes_chain_names_with_spaces():
    session = _StubSession(_StubResponse(json_body=[]))
    list(_source(session).fetch_chain_tvl("Arbitrum Nova"))
    assert session.calls[0][0].endswith("/historicalChainTvl/arbitrum%20nova")


def test_fetch_chain_tvl_filters_since_client_side():
    session = _StubSession(_StubResponse(json_body=_payload()))
    rows = list(_source(session).fetch_chain_tvl("ethereum", since=date(2026, 9, 24)))
    assert [r.observation_date for r in rows] == [
        date(2026, 9, 24),
        date(2026, 9, 25),
        date(2026, 9, 26),
    ]


def test_unknown_chain_404_raises_without_retry():
    session = _StubSession(_StubResponse(status_code=404, text="<html>404</html>"))
    with pytest.raises(DefiLlamaUnknownChainError):
        list(_source(session, max_retries=3).fetch_chain_tvl("notachain"))
    assert len(session.calls) == 1
    assert issubclass(DefiLlamaUnknownChainError, DataSourceError)


def test_transient_errors_are_retried_then_succeed():
    session = _StubSession(
        requests.ConnectionError("reset"),
        _StubResponse(status_code=503),
        _StubResponse(json_body=_payload()),
    )
    rows = list(_source(session, max_retries=3).fetch_chain_tvl("ethereum"))
    assert len(rows) == 10
    assert len(session.calls) == 3


def test_transient_errors_give_up_after_max_retries():
    session = _StubSession(_StubResponse(status_code=429), _StubResponse(status_code=429))
    with pytest.raises(requests.HTTPError):
        list(_source(session, max_retries=2).fetch_chain_tvl("ethereum"))
    assert len(session.calls) == 2


def test_client_error_is_not_retried():
    session = _StubSession(_StubResponse(status_code=400))
    with pytest.raises(requests.HTTPError):
        list(_source(session, max_retries=3).fetch_chain_tvl("ethereum"))
    assert len(session.calls) == 1


def test_undecodable_body_raises_response_error_without_retry():
    session = _StubSession(_StubResponse(text="<html>maintenance</html>"))
    with pytest.raises(DefiLlamaResponseError):
        list(_source(session, max_retries=3).fetch_chain_tvl("ethereum"))
    assert len(session.calls) == 1


def test_price_and_fundamental_capabilities_are_unsupported_soft_fails():
    src = _source(_StubSession())
    with pytest.raises(UnsupportedCapabilityError):
        list(src.fetch_prices("ETH-USD.CC"))
    with pytest.raises(UnsupportedCapabilityError):
        src.fetch_fundamentals("ETH-USD.CC")
    with pytest.raises(UnsupportedCapabilityError):
        src.list_tickers("CC")
    assert src.source_id == "defillama"


def test_base_default_chain_tvl_raises_unsupported():
    class _Minimal(DataSource):
        source_id = "minimal"

        def list_tickers(self, exchange):
            return []

        def fetch_prices(self, ticker, since=None, until=None):
            return []

        def fetch_fundamentals(self, ticker):
            raise NotImplementedError

    with pytest.raises(UnsupportedCapabilityError):
        list(_Minimal().fetch_chain_tvl("ethereum"))
    assert issubclass(UnsupportedCapabilityError, DataSourceError)

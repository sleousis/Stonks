"""Unit test for EodhdDataSource.list_tickers — verifies that both active and
delisted tickers are merged in a single call, since ``delisted=1`` returns
*only* delisted entries (disjoint from the default active-only response)."""

from __future__ import annotations

import json
from typing import Any

from stonks.ingest.sources.eodhd import EodhdDataSource


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


class _DelistedAwareSession:
    """Routes by the presence of the ``delisted`` query param so the two
    list-tickers calls land on different stub responses."""

    def __init__(self, active: list[dict], delisted: list[dict]):
        self._active = active
        self._delisted = delisted
        self.calls: list[dict[str, str]] = []

    def get(self, url, params=None, timeout=None):
        self.calls.append(dict(params or {}))
        if (params or {}).get("delisted") == "1":
            return _Response(200, self._delisted)
        return _Response(200, self._active)


def test_list_tickers_merges_active_and_delisted():
    active = [
        {"Code": "AAPL", "Name": "Apple Inc", "Exchange": "NASDAQ"},
        {"Code": "MSFT", "Name": "Microsoft", "Exchange": "NASDAQ"},
    ]
    delisted = [
        {"Code": "AAAB", "Name": "Admiralty Bancorp Inc", "Exchange": "NASDAQ"},
        {"Code": "ENRN", "Name": "Enron Corp", "Exchange": "NYSE"},
    ]
    session = _DelistedAwareSession(active=active, delisted=delisted)
    source = EodhdDataSource(api_key="k", session=session)  # type: ignore[arg-type]

    tickers = source.list_tickers("US")

    assert tickers == ["AAPL.US", "MSFT.US", "AAAB.US", "ENRN.US"]
    # exactly two HTTP calls: one without delisted, one with delisted=1
    assert len(session.calls) == 2
    delisted_flags = [c.get("delisted") for c in session.calls]
    assert delisted_flags.count("1") == 1
    assert delisted_flags.count(None) == 1


def test_list_tickers_dedupes_when_vendor_returns_overlap():
    """Defensive: vendor sets are disjoint today, but if a code ever appears
    in both responses we should not yield it twice."""
    active = [{"Code": "AAPL", "Exchange": "NASDAQ"}]
    delisted = [{"Code": "AAPL", "Exchange": "NASDAQ"}]
    session = _DelistedAwareSession(active=active, delisted=delisted)
    source = EodhdDataSource(api_key="k", session=session)  # type: ignore[arg-type]

    assert source.list_tickers("US") == ["AAPL.US"]


def test_list_tickers_tolerates_one_side_missing():
    """If the delisted call returns a non-list payload (e.g. transport error
    surfaced as an empty dict by upstream code), we still return the active
    tickers rather than raising."""
    active = [{"Code": "AAPL", "Exchange": "NASDAQ"}]
    session = _DelistedAwareSession(active=active, delisted={})  # type: ignore[arg-type]
    source = EodhdDataSource(api_key="k", session=session)  # type: ignore[arg-type]

    assert source.list_tickers("US") == ["AAPL.US"]


def test_list_tickers_warns_loudly_when_delisted_leg_returns_non_list(capsys):
    """I6: silently swallowing a failed delisted call would re-introduce
    survivorship bias. The warning log carries ``exchange=`` and an explicit
    survivorship-bias note so the user can spot the gap."""
    active = [{"Code": "AAPL", "Exchange": "NASDAQ"}]
    session = _DelistedAwareSession(active=active, delisted={})  # type: ignore[arg-type]
    source = EodhdDataSource(api_key="k", session=session)  # type: ignore[arg-type]

    source.list_tickers("US")
    out = capsys.readouterr().out
    matches = [line for line in out.splitlines() if "list_tickers.unexpected_payload_shape" in line]
    assert matches, f"expected a payload-shape warning; got {out!r}"
    line = matches[0]
    assert '"exchange": "US"' in line
    assert '"leg": "delisted"' in line
    assert "survivorship" in line

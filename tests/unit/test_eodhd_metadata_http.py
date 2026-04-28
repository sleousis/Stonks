"""Unit test for EodhdDataSource.fetch_metadata — exercises the full HTTP
dispatch (5 endpoints) through an injected fake session, no network."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from stonks.ingest.sources.eodhd import EodhdAllEndpointsFailedError, EodhdDataSource

FIXTURES = Path(__file__).parent.parent / "fixtures" / "eodhd"


class _Response:
    def __init__(self, status: int, body: Any):
        self.status_code = status
        self._body = body

    @property
    def text(self) -> str:
        return self._body if isinstance(self._body, str) else json.dumps(self._body)

    def json(self) -> Any:
        if isinstance(self._body, str):
            return json.loads(self._body)
        return self._body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            from requests.exceptions import HTTPError

            raise HTTPError(f"{self.status_code} error", response=self)


class _RoutingSession:
    """Fake session that routes by URL substring to a stub response."""

    def __init__(self, routes: dict[str, _Response]):
        self._routes = routes
        self.calls: list[str] = []

    def get(self, url, params=None, timeout=None):
        self.calls.append(url)
        for needle, response in self._routes.items():
            if needle in url:
                return response
        return _Response(404, f"no route matched {url}")


def _load(name: str):
    return json.loads((FIXTURES / name).read_text())


def test_fetch_metadata_assembles_full_bundle_from_all_endpoints():
    session = _RoutingSession(
        {
            "/fundamentals/": _Response(200, _load("aapl_fundamentals.json")),
            "/div/": _Response(200, _load("aapl_dividends.json")),
            "/splits/": _Response(200, _load("aapl_splits.json")),
            "/historical-market-cap/": _Response(200, _load("aapl_market_cap.json")),
            "/news": _Response(200, _load("aapl_news.json")),
            "/insider-transactions": _Response(200, _load("aapl_insider.json")),
            "/sentiments": _Response(200, _load("aapl_sentiments.json")),
        }
    )
    source = EodhdDataSource(api_key="k", session=session)  # type: ignore[arg-type]
    bundle = source.fetch_metadata("AAPL.US")

    # Static profile
    assert bundle.profile is not None
    assert bundle.profile.name == "Apple Inc"
    assert bundle.profile.cusip == "037833100"
    assert bundle.profile.gic_sector == "Information Technology"
    # Volatile snapshot lives on the new TickerSnapshotRow
    assert bundle.ticker_snapshot is not None
    assert bundle.ticker_snapshot.beta == 1.25
    # Standalone-endpoint fields
    assert len(bundle.dividends) == 2
    assert len(bundle.splits) == 5
    assert len(bundle.market_cap_history) == 4
    assert len(bundle.news) == 2
    assert len(bundle.insider_transactions) == 2
    assert len(bundle.news_sentiment) == 2
    # New fundamentals-derived fields
    assert len(bundle.earnings_announcements) == 2
    assert len(bundle.analyst_forecasts) == 2
    assert len(bundle.analyst_ratings) == 1
    assert len(bundle.institutional_holders) == 4
    assert bundle.esg_snapshot is not None
    assert bundle.esg_snapshot.environment_score == 0.7
    assert len(bundle.esg_activities) == 5
    assert len(bundle.cross_listings) == 2
    assert len(bundle.officers) == 3
    # full history from fundamentals.outstandingShares
    assert len(bundle.shares_outstanding) == 5
    assert len(bundle.employee_count) == 1


def test_fetch_metadata_tolerates_free_tier_403_on_some_endpoints():
    session = _RoutingSession(
        {
            "/fundamentals/": _Response(200, _load("aapl_fundamentals.json")),
            "/div/": _Response(403, "Only EOD data allowed for free users."),
            "/splits/": _Response(403, "Only EOD data allowed."),
            "/historical-market-cap/": _Response(403, "Only EOD data allowed."),
            "/news": _Response(403, "Only EOD data allowed for free users."),
            "/insider-transactions": _Response(403, "Only EOD data allowed."),
            "/sentiments": _Response(403, "Only EOD data allowed."),
        }
    )
    source = EodhdDataSource(api_key="k", max_retries=1, session=session)  # type: ignore[arg-type]
    bundle = source.fetch_metadata("AAPL.US")

    # fundamentals-derived fields still present
    assert bundle.profile is not None
    assert bundle.ticker_snapshot is not None
    assert len(bundle.analyst_ratings) == 1
    assert len(bundle.earnings_announcements) == 2
    assert len(bundle.analyst_forecasts) == 2
    assert len(bundle.institutional_holders) == 4
    assert len(bundle.officers) == 3
    # paid-only standalone-endpoint fields silently empty
    assert bundle.dividends == ()
    assert bundle.splits == ()
    assert bundle.market_cap_history == ()
    assert bundle.news == ()
    assert bundle.insider_transactions == ()
    assert bundle.news_sentiment == ()


def test_fetch_metadata_tolerates_complete_free_tier():
    session = _RoutingSession(
        {
            "/fundamentals/": _Response(403, "Only EOD data allowed."),
            "/div/": _Response(403, "Only EOD data allowed."),
            "/splits/": _Response(403, "Only EOD data allowed."),
            "/historical-market-cap/": _Response(403, "Only EOD data allowed."),
            "/news": _Response(403, "Only EOD data allowed."),
            "/insider-transactions": _Response(403, "Only EOD data allowed."),
            "/sentiments": _Response(403, "Only EOD data allowed."),
        }
    )
    source = EodhdDataSource(api_key="k", max_retries=1, session=session)  # type: ignore[arg-type]
    bundle = source.fetch_metadata("AAPL.US")

    # completely empty bundle, no exception
    assert bundle.profile is None
    assert bundle.ticker_snapshot is None
    assert bundle.dividends == ()
    assert bundle.splits == ()
    assert bundle.market_cap_history == ()
    assert bundle.news == ()
    assert bundle.earnings_announcements == ()
    assert bundle.analyst_forecasts == ()
    assert bundle.institutional_holders == ()
    assert bundle.cross_listings == ()
    assert bundle.officers == ()


class _AlwaysRaisesSession:
    """Fake session that raises a transport-class error on every GET, so we
    can verify ``fetch_metadata`` distinguishes "vendor down" from "vendor
    said no data" — only the former should escalate to a hard failure."""

    def __init__(self, exc: Exception):
        self._exc = exc
        self.calls: list[str] = []

    def get(self, url, params=None, timeout=None):
        self.calls.append(url)
        raise self._exc


def test_fetch_metadata_raises_when_all_endpoints_fail_with_transport_error():
    # A real outage where every single sub-fetch hit a transport error.
    # Returning an empty bundle would silently mark the ticker "OK" upstream;
    # raising lets the per-ticker soft-fail record this honestly.
    import requests

    session = _AlwaysRaisesSession(requests.ConnectionError("boom"))
    source = EodhdDataSource(api_key="k", max_retries=1, session=session)  # type: ignore[arg-type]
    with pytest.raises(EodhdAllEndpointsFailedError):
        source.fetch_metadata("AAPL.US")


def test_fetch_metadata_does_not_raise_for_all_free_tier_blocks():
    # Mirror image of the previous test: every endpoint blocked is a normal
    # free-tier steady state, NOT an outage. The bundle should be empty
    # and ``EodhdAllEndpointsFailedError`` must NOT fire — otherwise every
    # free-tier user's ingest would crash.
    session = _RoutingSession(
        {
            "/fundamentals/": _Response(403, "Only EOD data allowed."),
            "/div/": _Response(403, "Only EOD data allowed."),
            "/splits/": _Response(403, "Only EOD data allowed."),
            "/historical-market-cap/": _Response(403, "Only EOD data allowed."),
            "/news": _Response(403, "Only EOD data allowed."),
            "/insider-transactions": _Response(403, "Only EOD data allowed."),
            "/sentiments": _Response(403, "Only EOD data allowed."),
        }
    )
    source = EodhdDataSource(api_key="k", max_retries=1, session=session)  # type: ignore[arg-type]
    # Must not raise.
    bundle = source.fetch_metadata("AAPL.US")
    assert bundle.profile is None
    assert bundle.dividends == ()


def test_fetch_metadata_log_lines_carry_ticker_and_endpoint(capsys):
    # C4: when an endpoint fails, the structured log line must include the
    # ticker and the endpoint name so debugging "why does X have no data?"
    # is possible from logs alone. structlog renders to stdout in JSON, so
    # we assert on the captured output rather than caplog records.
    import requests

    routes = {
        "/fundamentals/": _Response(200, _load("aapl_fundamentals.json")),
        "/div/": _Response(200, _load("aapl_dividends.json")),
        "/splits/": _Response(200, _load("aapl_splits.json")),
        "/historical-market-cap/": _Response(200, _load("aapl_market_cap.json")),
        "/insider-transactions": _Response(200, _load("aapl_insider.json")),
        "/sentiments": _Response(200, _load("aapl_sentiments.json")),
    }

    class _NewsRaisingSession(_RoutingSession):
        def get(self, url, params=None, timeout=None):
            self.calls.append(url)
            if "/news" in url and "sentiments" not in url:
                raise requests.ConnectionError("vendor unreachable")
            return super().get(url, params=params, timeout=timeout)

    session = _NewsRaisingSession(routes)
    source = EodhdDataSource(api_key="k", max_retries=1, session=session)  # type: ignore[arg-type]
    source.fetch_metadata("AAPL.US")
    out = capsys.readouterr().out
    # Find the line for our soft-failed sub-fetch and verify both context
    # keys are present (ticker + endpoint name).
    matches = [line for line in out.splitlines() if "eodhd.metadata.skipped_error" in line]
    assert matches, f"no skipped_error event emitted; got {out!r}"
    line = matches[0]
    assert '"ticker": "AAPL.US"' in line
    assert '"endpoint": "news"' in line

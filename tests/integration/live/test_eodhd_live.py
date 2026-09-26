"""Live contract test against real EODHD. Gated behind STONKS_RUN_LIVE_TESTS=1
and the presence of EODHD_API_KEY so the default pytest run is hermetic.

Uses AAPL.US (free-tier eligible) and a recent window (free tier caps history
to ~1 year). Fundamentals are paid-only; the test tolerates either success OR
the canonical EodhdFreeTierError so this file works on both subscription tiers.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest

from stonks.ingest.schemas import RawPriceBar
from stonks.ingest.sources.eodhd import EodhdDataSource, EodhdFreeTierError

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("STONKS_RUN_LIVE_TESTS") != "1",
        reason="set STONKS_RUN_LIVE_TESTS=1 to enable live API tests",
    ),
    pytest.mark.skipif(
        not os.environ.get("EODHD_API_KEY"),
        reason="EODHD_API_KEY not set",
    ),
]


@pytest.fixture
def source():
    return EodhdDataSource(api_key=os.environ["EODHD_API_KEY"])


def test_live_fetch_prices_aapl(source):
    today = datetime.now(UTC).date()
    # stay well inside the free-tier one-year window
    since = today - timedelta(days=14)
    until = today - timedelta(days=1)

    bars = list(source.fetch_prices("AAPL.US", since=since, until=until))

    assert bars, "expected at least one price bar within the last 14 days"
    assert all(isinstance(b, RawPriceBar) for b in bars)
    assert all(b.ticker == "AAPL.US" for b in bars)
    assert all(since <= b.date <= until for b in bars)


def test_live_fundamentals_either_succeeds_or_reports_free_tier(source):
    try:
        bundle = source.fetch_fundamentals("AAPL.US")
    except EodhdFreeTierError:
        pytest.skip("fundamentals endpoint is paid-only; free tier returned the expected error")
    else:
        assert bundle.income, "paid tier should yield at least one income statement row"
        assert bundle.balance, "paid tier should yield at least one balance sheet row"
        assert bundle.cashflow, "paid tier should yield at least one cash flow row"


def test_live_metadata_carries_extended_surface(source):
    """Smoke-test the full metadata bundle on a paid plan: every new field
    we model should be populated for AAPL. Free-tier runs leave most of
    the bundle empty (soft-fail) — we assert that the call returned
    without raising and that at least the static profile landed.
    """
    bundle = source.fetch_metadata("AAPL.US")

    # Profile is sourced from /api/fundamentals; on paid tier it must be present.
    if bundle.profile is None:
        pytest.skip("fundamentals blocked on free tier — bundle is empty")

    # Static identifiers (paid-tier expectation; we assert presence, not
    # exact values, since vendor data can drift).
    assert bundle.profile.cusip, "expected CUSIP populated"
    assert bundle.profile.cik, "expected CIK populated"
    assert bundle.profile.gic_sector, "expected GIC sector populated"
    assert bundle.profile.security_type == "common_stock"

    # Volatile snapshot row should be populated (beta + ownership).
    assert bundle.ticker_snapshot is not None
    assert bundle.ticker_snapshot.beta is not None

    # New fundamentals-derived collections (size > 0 on paid tier).
    assert bundle.earnings_announcements, "expected historical earnings announcements"
    assert bundle.analyst_forecasts, "expected analyst forecast rows"
    assert bundle.analyst_ratings, "expected analyst ratings snapshot"
    assert bundle.institutional_holders, "expected institutional holders top-N"
    assert bundle.cross_listings or bundle.officers, (
        "expected cross_listings or officers populated for AAPL"
    )


def test_live_fetch_macro_indicator_either_succeeds_or_reports_paywall(source):
    """Macro indicators sit under the Fundamentals subscription per
    EODHD's docs (https://eodhd.com/financial-apis/macroeconomics-data-api/),
    so a non-fundamentals key gets blocked. Empirically EODHD signals
    that paywall with **HTTP 404** on this endpoint (not the usual 403 +
    text marker we see on /fundamentals), so we tolerate either: a real
    time series on paid keys, or any of {EodhdFreeTierError, HTTP 404}
    on plans that don't include macro."""
    import requests

    from stonks.ingest.schemas import MacroIndicatorRow

    try:
        rows = list(source.fetch_macro_indicator(country_iso="USA", indicator="real_gdp_total"))
    except EodhdFreeTierError:
        pytest.skip("macro endpoint blocked: vendor returned the canonical free-tier marker")
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 404:
            pytest.skip(
                "macro endpoint blocked: vendor returned 404 (paywall on non-fundamentals plans)"
            )
        raise
    else:
        assert rows, "paid tier should yield at least one observation for USA real_gdp_total"
        assert all(isinstance(r, MacroIndicatorRow) for r in rows)
        assert all(r.country_iso == "USA" for r in rows)
        assert all(r.indicator == "real_gdp_total" for r in rows)
        # Series stretches back to ~1960 per vendor docs; assert we got
        # multiple decades' worth rather than a single point.
        years = {r.observation_date.year for r in rows}
        assert len(years) >= 10, f"expected a multi-decade series, got years={sorted(years)}"

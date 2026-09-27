"""Live contract test of the EODHD options API (roadmap 17.1). Gated behind
STONKS_RUN_LIVE_TESTS=1 and EODHD_API_KEY. Options are a separate
Marketplace subscription, so a key without it skips instead of failing."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest

from stonks.ingest.sources.base import DataSourceError
from stonks.ingest.sources.eodhd import EodhdDataSource

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("STONKS_RUN_LIVE_TESTS") != "1",
        reason="set STONKS_RUN_LIVE_TESTS=1 to enable live API tests",
    ),
    pytest.mark.skipif(not os.environ.get("EODHD_API_KEY"), reason="EODHD_API_KEY not set"),
]


def test_live_option_quotes_map_to_our_rows():
    source = EodhdDataSource(api_key=os.environ["EODHD_API_KEY"])
    day = datetime.now(UTC).date() - timedelta(days=5)
    try:
        rows = list(source.fetch_option_quotes("AAPL.US", day, day + timedelta(days=3)))
    except DataSourceError as exc:
        pytest.skip(f"no options subscription on this key: {exc}")
    assert rows, "expected option quotes for AAPL in the last few days"
    assert all(r.underlying == "AAPL.US" for r in rows)
    assert all(r.contract_id.startswith("AAPL.US:") for r in rows)
    assert any(r.iv is not None for r in rows)

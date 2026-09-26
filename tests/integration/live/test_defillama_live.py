"""Live contract test against the real DefiLlama API (no key needed). Gated
behind STONKS_RUN_LIVE_TESTS=1 so the default pytest run is hermetic."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest

from stonks.ingest.sources.defillama import DefiLlamaDataSource, DefiLlamaUnknownChainError

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("STONKS_RUN_LIVE_TESTS") != "1",
        reason="set STONKS_RUN_LIVE_TESTS=1 to enable live API tests",
    ),
]


@pytest.fixture
def source():
    return DefiLlamaDataSource()


def test_live_ethereum_tvl_is_a_recent_daily_series(source):
    today = datetime.now(UTC).date()
    rows = list(source.fetch_chain_tvl("Ethereum", since=today - timedelta(days=30)))
    assert len(rows) >= 20
    assert all(r.chain == "ethereum" and r.source == "defillama" for r in rows)
    assert all(r.tvl_usd is not None and r.tvl_usd > 1e9 for r in rows)
    dates = [r.observation_date for r in rows]
    assert dates == sorted(set(dates))
    assert (today - dates[-1]).days <= 3


def test_live_unknown_chain_soft_fails(source):
    with pytest.raises(DefiLlamaUnknownChainError):
        list(source.fetch_chain_tvl("definitely-not-a-chain-xyz"))

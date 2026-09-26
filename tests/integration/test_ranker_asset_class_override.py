"""BL-26: an ``asset_classes`` instance param overrides the class's
``applicable_asset_classes`` in the Ranker's asset-class filter."""

from __future__ import annotations

from datetime import date

import pytest

from stonks.core.protocols import SurvivalReport
from stonks.ingest.pipeline import _rows_to_df
from stonks.ingest.schemas import TickerProfile
from stonks.production.ranker import Ranker
from stonks.registry.store import StrategyRegistry
from stonks.store.state import SqliteState
from stonks.strategies.examples.buy_and_hold import BuyAndHold
from tests.fixtures.governance import seed_status


def _rank(tmp_path, lake, params: dict) -> set[str]:
    lake.upsert_instrument_profile(
        _rows_to_df(
            [
                TickerProfile(id="UP.US", asset_class="equity"),
                TickerProfile(id="DOWN.US", asset_class="equity"),
            ]
        )
    )
    with SqliteState(tmp_path / "state.sqlite") as state:
        state.migrate()
        registry = StrategyRegistry(state=state, artifacts_dir=tmp_path / "artifacts")
        sid = registry.register(
            BuyAndHold(params),
            reports=[SurvivalReport(test_id="oos", passed=True, metrics={})],
        )
        seed_status(registry, sid, "active")
        ranker = Ranker(registry=registry, lake=lake, universe=["UP.US"], threshold=0.0)
        return {ticker for _, _, ticker in ranker.rank(as_of=date(2026, 3, 20))}


def test_default_classes_admit_the_equity_ticker(tmp_path, lake_trending):
    assert _rank(tmp_path, lake_trending, {"ticker": "UP.US"}) == {"UP.US"}


def test_override_excludes_tickers_outside_it(tmp_path, lake_trending):
    picks = _rank(tmp_path, lake_trending, {"ticker": "UP.US", "asset_classes": ["crypto"]})
    assert picks == set()


def test_empty_override_is_rejected():
    with pytest.raises(ValueError, match="asset_classes"):
        BuyAndHold({"ticker": "UP.US", "asset_classes": []})

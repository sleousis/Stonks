"""Test helper for seeding a strategy's status through the audited path.

Tests that need an ``active`` (or ``retired``) strategy go through
``StrategyRegistry.set_status`` like production code does, using a logged
override instead of a go-live report.
"""

from __future__ import annotations

from stonks.registry.store import StrategyRegistry

TEST_ACTOR = "test"
TEST_REASON = "test fixture seeds this status directly"


def seed_status(registry: StrategyRegistry, strategy_id: str, status: str) -> None:
    registry.set_status(
        strategy_id, status, actor=TEST_ACTOR, reason=TEST_REASON, override=status == "active"
    )

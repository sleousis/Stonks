"""Test helper for seeding a strategy's status through the audited path.

Tests that need an ``active`` (or ``retired``) strategy go through
``StrategyRegistry.set_status`` like production code does, using a logged
override instead of a go-live report. With ``default_book=True`` activating
also subscribes the default book in paper (``accounts.default_book``), like
a promotion through the service layer (``app.strategies.change_status``).
Tests that run the tick through an entrypoint need that.
"""

from __future__ import annotations

from stonks.accounts.default_book import ensure_default_subscription
from stonks.accounts.models import Mode
from stonks.registry.store import StrategyRegistry

TEST_ACTOR = "test"
TEST_REASON = "test fixture seeds this status directly"


def seed_status(
    registry: StrategyRegistry, strategy_id: str, status: str, *, default_book: bool = False
) -> None:
    registry.set_status(
        strategy_id, status, actor=TEST_ACTOR, reason=TEST_REASON, override=status == "active"
    )
    if status == "active" and default_book:
        ensure_default_subscription(registry._state, strategy_id, Mode.PAPER)

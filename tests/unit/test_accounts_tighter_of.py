"""``tighter_of``: per-portfolio and per-subscription risk overrides can only
tighten the global policy (design section 4, principle P28)."""

from __future__ import annotations

import random

import pytest
from pydantic import ValidationError

from stonks.accounts.book import MERGE_RULES, tighter_of
from stonks.config import RiskPolicy


def test_every_risk_policy_field_has_a_merge_rule():
    # A new RiskPolicy field must say how it tightens before it can be merged.
    assert set(MERGE_RULES) == set(RiskPolicy.model_fields)


def test_no_overrides_returns_the_base_unchanged():
    base = RiskPolicy(max_weight_per_ticker=0.3, enabled=False)
    assert tighter_of(base) == base
    assert tighter_of(base, None, {}) == base


def test_empty_override_does_not_enable_a_disabled_policy():
    # "{}" in risk_policy_json must be a no-op, or the single-owner tick
    # would change behaviour when the global policy is disabled.
    base = RiskPolicy(enabled=False)
    assert tighter_of(base, {}, RiskPolicy()).enabled is False


def test_overrides_tighten_each_field():
    base = RiskPolicy(
        max_open_positions=10,
        max_weight_per_ticker=0.5,
        max_weight_per_asset_class={"crypto": 0.3},
        cash_buffer_fraction=0.05,
        min_order_notional=10.0,
    )
    merged = tighter_of(
        base,
        {"max_open_positions": 5, "max_weight_per_ticker": 0.2, "cash_buffer_fraction": 0.1},
        {"max_weight_per_asset_class": {"crypto": 0.1, "bond": 0.5}, "min_order_notional": 50},
    )
    assert merged.max_open_positions == 5
    assert merged.max_weight_per_ticker == 0.2
    assert merged.max_weight_per_asset_class == {"crypto": 0.1, "bond": 0.5}
    assert merged.cash_buffer_fraction == 0.1
    assert merged.min_order_notional == 50.0


def test_overrides_cannot_loosen():
    base = RiskPolicy(
        enabled=True,
        max_open_positions=5,
        max_weight_per_ticker=0.2,
        max_weight_per_asset_class={"crypto": 0.1},
        cash_buffer_fraction=0.1,
        min_order_notional=50.0,
    )
    loose = {
        "enabled": False,
        "max_open_positions": None,
        "max_weight_per_ticker": 1.0,
        "max_weight_per_asset_class": {"crypto": 1.0},
        "cash_buffer_fraction": 0.0,
        "min_order_notional": 0.0,
    }
    assert tighter_of(base, loose) == base


def test_override_can_enable_risk():
    assert tighter_of(RiskPolicy(enabled=False), {"enabled": True}).enabled is True


def test_invalid_override_is_rejected():
    with pytest.raises(ValidationError):
        tighter_of(RiskPolicy(), {"max_weight_per_ticker": 2.0})
    with pytest.raises(ValidationError):
        tighter_of(RiskPolicy(), {"no_such_limit": 1})


def _random_partial(rng: random.Random) -> dict:
    out: dict = {}
    if rng.random() < 0.5:
        out["enabled"] = rng.random() < 0.5
    if rng.random() < 0.5:
        out["max_open_positions"] = rng.choice([None, rng.randint(0, 20)])
    if rng.random() < 0.5:
        out["max_weight_per_ticker"] = round(rng.random(), 3)
    if rng.random() < 0.5:
        out["max_weight_per_asset_class"] = {
            c: round(rng.random(), 3)
            for c in rng.sample(["equity", "crypto", "bond", "commodity"], rng.randint(0, 3))
        }
    if rng.random() < 0.5:
        out["cash_buffer_fraction"] = round(rng.random(), 3)
    if rng.random() < 0.5:
        out["min_order_notional"] = round(rng.random() * 100, 2)
    return out


def _no_looser(merged: RiskPolicy, other: RiskPolicy, explicit: set[str]) -> None:
    if "enabled" in explicit and other.enabled:
        assert merged.enabled
    if other.max_open_positions is not None:
        assert merged.max_open_positions is not None
        assert merged.max_open_positions <= other.max_open_positions
    assert merged.max_weight_per_ticker <= other.max_weight_per_ticker
    for cls, cap in other.max_weight_per_asset_class.items():
        assert merged.max_weight_per_asset_class[cls] <= cap
    assert merged.cash_buffer_fraction >= other.cash_buffer_fraction
    assert merged.min_order_notional >= other.min_order_notional


def test_property_merged_is_never_looser_than_any_input():
    rng = random.Random(15_2)
    for _ in range(500):
        base = RiskPolicy.model_validate(_random_partial(rng))
        overrides = [_random_partial(rng) for _ in range(rng.randint(0, 3))]
        merged = tighter_of(base, *overrides)
        _no_looser(merged, base, set(RiskPolicy.model_fields))
        for o in overrides:
            _no_looser(merged, RiskPolicy.model_validate(o), set(o))
        # Idempotent and order-independent over the overrides.
        assert tighter_of(merged, *overrides) == merged
        assert tighter_of(base, *reversed(overrides)) == merged

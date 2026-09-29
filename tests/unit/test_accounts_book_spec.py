"""``BookSpec.default``: today's single book, built from config alone."""

from __future__ import annotations

import dataclasses

import pytest

from stonks.accounts.book import BookSpec, merge_construction
from stonks.accounts.models import DEFAULT_PORTFOLIO_ID
from stonks.config import RiskPolicy, Settings


def test_default_book_mirrors_config():
    settings = Settings()
    settings.production.risk = RiskPolicy(max_weight_per_ticker=0.25)
    settings.production.universe = ["A.US", "B.US"]
    settings.production.initial_cash = 5_000.0
    book = BookSpec.default(settings)
    assert book.portfolio_id == DEFAULT_PORTFOLIO_ID
    assert book.strategy_weights is None  # equal over whatever signals arrive
    assert book.risk == settings.production.risk
    assert dict(book.risk_overrides) == {}
    assert book.allow_short is False
    assert book.broker == "simulated"
    assert book.initial_cash == 5_000.0
    assert book.universe == ("A.US", "B.US")
    assert dict(book.construction) == {}


def test_default_book_uses_the_connection_for_alpaca():
    settings = Settings()
    settings.brokers.kind = "alpaca"
    assert BookSpec.default(settings).broker == "connection"


def test_book_spec_is_frozen():
    book = BookSpec.default(Settings())
    with pytest.raises(dataclasses.FrozenInstanceError):
        book.allow_short = True  # type: ignore[misc]
    with pytest.raises(TypeError):
        book.construction["long_only"] = False  # type: ignore[index]


def test_merge_construction_only_tightens_shared_knobs():
    base = {"constructor": "vol_target", "long_only": True, "max_gross": 0.8}
    merged = merge_construction(
        base, {"long_only": False, "max_gross": 0.5, "constructor": "equal"}
    )
    assert merged["long_only"] is True
    assert merged["max_gross"] == 0.5
    # Non-risk knobs (which constructor, its parameters) are the portfolio's choice.
    assert merged["constructor"] == "equal"
    assert merge_construction(base, {"max_gross": 1.0})["max_gross"] == 0.8


def test_merge_construction_tightens_against_the_default_when_global_is_silent():
    """A short book's portfolio cannot pick max_gross above the effective
    default (1.0) just because the global settings leave it unset (P26,
    P28). Nested constructor params are held to the same rule."""
    assert merge_construction({}, {"max_gross": 4.0})["max_gross"] == 1.0
    assert (
        merge_construction({"method": "equal_weight_top_n"}, {"max_gross": 0.6})["max_gross"] == 0.6
    )
    nested = merge_construction({}, {"params": {"max_gross": 3.0, "top_n": 5}})
    assert nested.get("max_gross", 1.0) <= 1.0
    assert nested.get("params", {}).get("max_gross", 1.0) <= 1.0
    # the global setting still widens the room when an operator sets it
    assert merge_construction({"max_gross": 2.0}, {"max_gross": 1.5})["max_gross"] == 1.5

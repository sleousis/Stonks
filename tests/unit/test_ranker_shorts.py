"""The ranker keeps short scores apart (roadmap 16.3)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from stonks.production.ranker import Ranker, SignalSet

UNIVERSE = ["A", "B", "C"]
SCORES = {"A": 0.3, "B": -0.2, "C": -0.05}


class _Scorer:
    applicable_asset_classes = ("equity",)

    def __init__(self, supports_short: bool) -> None:
        self.supports_short = supports_short

    def estimate_return(self, ticker: str, as_of: Any, lake: Any) -> float | None:
        return SCORES[ticker]


@dataclass
class _Handle:
    id: str
    class_path: str = "x:y"


class _Registry:
    def __init__(self, strategies: dict[str, Any]) -> None:
        self._strategies = strategies

    def list_all(self, status: str | None = None) -> list[_Handle]:
        return [_Handle(sid) for sid in self._strategies]

    def load(self, sid: str) -> Any:
        return self._strategies[sid]


class _Lake:
    def get_asset_classes(self, tickers: list[str]) -> dict[str, str]:
        return dict.fromkeys(tickers, "equity")


def _rank(allow_short: bool, threshold: float = 0.1) -> SignalSet:
    registry = _Registry({"ls": _Scorer(True), "lo": _Scorer(False)})
    ranker = Ranker(
        registry=registry,  # type: ignore[arg-type]
        lake=_Lake(),  # type: ignore[arg-type]
        universe=UNIVERSE,
        threshold=threshold,
        allow_short=allow_short,
    )
    return ranker.score(date(2026, 1, 2))


def test_long_only_ranker_drops_negative_scores():
    signals = _rank(allow_short=False)
    assert signals.scores == {"ls": {"A": 0.3}, "lo": {"A": 0.3}}
    assert signals.shorts == {}


def test_short_ranker_keeps_negatives_of_short_strategies_apart():
    signals = _rank(allow_short=True)
    # the long view is unchanged, so long-only books see the same scores
    assert signals.scores == {"ls": {"A": 0.3}, "lo": {"A": 0.3}}
    # |score| must clear the threshold: C (-0.05) stays out
    assert signals.shorts == {"ls": {"B": -0.2}}


def test_for_book_merges_shorts_only_for_short_books():
    signals = _rank(allow_short=True)
    assert signals.for_book(allow_short=False) == {"ls": {"A": 0.3}, "lo": {"A": 0.3}}
    # universe order is kept inside a strategy
    assert signals.for_book(allow_short=True) == {"ls": {"A": 0.3, "B": -0.2}, "lo": {"A": 0.3}}


def test_ranked_view_ignores_shorts():
    signals = _rank(allow_short=True)
    assert [p[2] for p in signals.ranked()] == ["A", "A"]


def test_merged_keeps_shorts_of_both_sets():
    a = SignalSet(as_of=date(2026, 1, 2), scores={"x": {}}, shorts={"x": {"B": -1.0}})
    b = SignalSet(as_of=date(2026, 1, 2), scores={"y": {}}, shorts={"y": {"C": -2.0}})
    assert a.merged(b).shorts == {"x": {"B": -1.0}, "y": {"C": -2.0}}

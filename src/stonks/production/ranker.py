"""Strategy ranker — iterates active strategies × universe and collects
(expected_return, strategy_id, ticker) tuples that clear a threshold.

Sorted descending by expected return.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from stonks.logging import get_logger
from stonks.registry.store import StrategyRegistry
from stonks.store.lake import DuckDBLake

_log = get_logger("stonks.production.ranker")


@dataclass(frozen=True)
class Pick:
    expected_return: float
    strategy_id: str
    ticker: str


class Ranker:
    def __init__(
        self,
        registry: StrategyRegistry,
        lake: DuckDBLake,
        universe: Sequence[str],
        threshold: float = 0.0,
    ) -> None:
        self._registry = registry
        self._lake = lake
        self._universe = list(universe)
        self._threshold = threshold

    def rank(self, as_of: date) -> list[tuple[float, str, str]]:
        handles = self._registry.list_active()
        picks: list[tuple[float, str, str]] = []
        for handle in handles:
            strategy = self._registry.load(handle.id)
            for ticker in self._universe:
                try:
                    r = strategy.estimate_return(ticker, as_of, self._lake)
                except Exception as exc:
                    _log.warning(
                        "ranker.estimate_return.failed",
                        strategy_id=handle.id,
                        ticker=ticker,
                        error=str(exc),
                    )
                    continue
                if r is None or r <= self._threshold:
                    continue
                picks.append((r, handle.id, ticker))
        picks.sort(key=lambda p: p[0], reverse=True)
        return picks

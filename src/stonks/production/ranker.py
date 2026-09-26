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
        # Resolve asset classes once per tick. Tickers without an
        # ``instruments`` row are treated as "asset class unknown" and
        # skipped with a single warning per tick — defaulting to equity
        # would route non-equity tickers (whose profile fetch may have
        # failed) to equity-only strategies, masking real ingestion
        # problems. To re-enable an unknown ticker, run
        # ``stonks ingest metadata`` for it.
        asset_classes = self._lake.get_asset_classes(self._universe)
        unknown_universe = [t for t in self._universe if t not in asset_classes]
        if unknown_universe:
            _log.warning(
                "ranker.unknown_asset_class.skipped",
                tickers=unknown_universe,
                count=len(unknown_universe),
                hint="run `stonks ingest metadata` to populate instrument profiles",
            )
        picks: list[tuple[float, str, str]] = []
        for handle in handles:
            # One broken strategy (renamed class path, corrupt artifact, …)
            # must not take down the whole tick; skip it and keep ranking.
            try:
                strategy = self._registry.load(handle.id)
            except Exception as exc:
                _log.warning(
                    "ranker.strategy_load.failed",
                    strategy_id=handle.id,
                    class_path=handle.class_path,
                    error=str(exc),
                )
                continue
            allowed = set(getattr(strategy, "applicable_asset_classes", ("equity",)))
            for ticker in self._universe:
                ticker_class = asset_classes.get(ticker)
                if ticker_class is None or ticker_class not in allowed:
                    continue
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

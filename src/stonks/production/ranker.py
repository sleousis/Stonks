"""The signal phase of a tick (BL-12, W2.1).

``Ranker`` scores the strategies of one registry status (``active`` by
default, ``shadow`` for the model books) over the universe, once per tick.
:meth:`Ranker.score` returns a :class:`SignalSet`: one raw
``ticker -> expected return`` map per strategy (above the threshold, in
registry then universe order) plus the strategy instances that computed
them. :meth:`Ranker.rank` keeps the flat list sorted descending by expected
return, for callers that want the old view.

:class:`StrategyPool` hands those instances to whatever decides next (each
portfolio's construction, the model books). The instance that scored is the
one that decides, so per-day state a strategy computes in
``estimate_return`` (StocksOnTheMove's ATRs and index filter, a rule
strategy's evaluations) reaches its ``decide``. When several consumers
decide with one strategy, each later one gets a copy taken before the first
decided, so state a ``decide`` keeps (entry prices, ...) never leaks from
one portfolio into another.
"""

from __future__ import annotations

import copy
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

from stonks.core.protocols import Strategy
from stonks.logging import get_logger
from stonks.registry.store import StrategyRegistry
from stonks.store.lake import DuckDBLake

_log = get_logger("stonks.production.ranker")

RankedPick = tuple[float, str, str]


@dataclass(frozen=True)
class Pick:
    expected_return: float
    strategy_id: str
    ticker: str


@dataclass(frozen=True)
class SignalSet:
    """What the signal phase produced for one ``as_of``.

    - ``scores``: ``strategy_id -> ticker -> raw expected return`` for every
      strategy that loaded, in registry order; tickers in universe order.
      An empty map means the strategy was scored and has no opinion today.
    - ``instances``: the strategy objects that computed ``scores``.
    """

    as_of: date
    scores: Mapping[str, Mapping[str, float]] = field(default_factory=dict)
    instances: Mapping[str, Strategy] = field(default_factory=dict)

    def ranked(self, strategy_ids: Sequence[str] | None = None) -> list[RankedPick]:
        """``(expected_return, strategy_id, ticker)`` sorted descending (a
        stable sort, so ties keep registry then universe order), optionally
        only for ``strategy_ids``."""
        wanted = None if strategy_ids is None else set(strategy_ids)
        picks = [
            (r, sid, ticker)
            for sid, scores in self.scores.items()
            if wanted is None or sid in wanted
            for ticker, r in scores.items()
        ]
        picks.sort(key=lambda p: p[0], reverse=True)
        return picks

    def merged(self, other: SignalSet) -> SignalSet:
        return SignalSet(
            as_of=self.as_of,
            scores={**self.scores, **other.scores},
            instances={**self.instances, **other.instances},
        )


class Ranker:
    def __init__(
        self,
        registry: StrategyRegistry,
        lake: DuckDBLake,
        universe: Sequence[str],
        threshold: float = 0.0,
        status: Literal["active", "shadow"] = "active",
    ) -> None:
        self._registry = registry
        self._status = status
        self._lake = lake
        self._universe = list(universe)
        self._threshold = threshold
        #: The last :meth:`rank`'s per-strategy view.
        self.signals: SignalSet | None = None

    def score(self, as_of: date) -> SignalSet:
        """Score every strategy of this status once; see :class:`SignalSet`."""
        self.rank(as_of=as_of)
        assert self.signals is not None
        return self.signals

    def rank(self, as_of: date) -> list[RankedPick]:
        handles = self._registry.list_all(status=self._status)
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
        scores: dict[str, dict[str, float]] = {}
        instances: dict[str, Strategy] = {}
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
            instances[handle.id] = strategy
            scores[handle.id] = self._score_one(handle.id, strategy, as_of, asset_classes)
        self.signals = SignalSet(as_of=as_of, scores=scores, instances=instances)
        return self.signals.ranked()

    def _score_one(
        self,
        strategy_id: str,
        strategy: Strategy,
        as_of: date,
        asset_classes: Mapping[str, str],
    ) -> dict[str, float]:
        allowed = set(getattr(strategy, "applicable_asset_classes", ("equity",)))
        out: dict[str, float] = {}
        for ticker in self._universe:
            ticker_class = asset_classes.get(ticker)
            if ticker_class is None or ticker_class not in allowed:
                continue
            try:
                r = strategy.estimate_return(ticker, as_of, self._lake)
            except Exception as exc:
                _log.warning(
                    "ranker.estimate_return.failed",
                    strategy_id=strategy_id,
                    ticker=ticker,
                    error=str(exc),
                )
                continue
            if r is None or r <= self._threshold:
                continue
            out[ticker] = r
        return out


class StrategyPool:
    """The strategy instances of one tick, handed to each consumer that
    decides with them (see the module doc).

    ``expect(sid, n)`` declares how many consumers will decide with ``sid``.
    The first :meth:`checkout` returns the scoring instance itself; when more
    consumers are expected, a copy is taken first and every later checkout
    gets its own copy of it. A strategy the signal phase didn't score (e.g.
    the owner of holdings in an exit-only decision) is loaded from the
    registry, and a load error propagates to the caller.
    """

    def __init__(self, registry: StrategyRegistry, lake: Any) -> None:
        self._registry = registry
        self._lake = lake
        self._instances: dict[str, Strategy] = {}
        self._pristine: dict[str, Strategy] = {}
        self._expected: Counter[str] = Counter()
        self._uses: Counter[str] = Counter()

    def add(self, signals: SignalSet) -> None:
        for sid, instance in signals.instances.items():
            self._instances.setdefault(sid, instance)

    def expect(self, strategy_id: str, consumers: int = 1) -> None:
        self._expected[strategy_id] += consumers

    def checkout(self, strategy_id: str) -> Strategy:
        instance = self._instances.get(strategy_id)
        if instance is None:
            instance = self._registry.load(strategy_id)
            self._instances[strategy_id] = instance
        uses = self._uses[strategy_id]
        self._uses[strategy_id] += 1
        if uses == 0:
            if self._expected[strategy_id] > 1:
                self._pristine[strategy_id] = self._fork(strategy_id, instance)
            return instance
        base = self._pristine.get(strategy_id)
        if base is None:
            # More consumers than declared: the scoring instance may already
            # carry a decision's state, so copy the registry's instead.
            _log.warning("strategy_pool.unexpected_consumer", strategy_id=strategy_id)
            base = self._registry.load(strategy_id)
            self._pristine[strategy_id] = base
        return self._fork(strategy_id, base)

    def _fork(self, strategy_id: str, instance: Strategy) -> Strategy:
        """A deep copy that shares the lake (never copied)."""
        return copy.deepcopy(instance, {id(self._lake): self._lake})

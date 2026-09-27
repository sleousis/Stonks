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

Shorts (roadmap 16.3): with ``allow_short`` the ranker also keeps the
negative scores (``score < -threshold``) of strategies that support shorts,
in :attr:`SignalSet.shorts`, apart from ``scores``. Long-only books read
``scores`` exactly as before; a book that may short reads
:meth:`SignalSet.for_book`. Strategies that may short are scored serially.
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
    - ``shorts``: ``strategy_id -> ticker -> negative score`` of strategies
      that support shorts, only when the ranker ran with ``allow_short``.
    """

    as_of: date
    scores: Mapping[str, Mapping[str, float]] = field(default_factory=dict)
    instances: Mapping[str, Strategy] = field(default_factory=dict)
    shorts: Mapping[str, Mapping[str, float]] = field(default_factory=dict)
    #: The universe the scores are ordered by (merges keep this order).
    universe: tuple[str, ...] = ()

    def for_book(self, allow_short: bool) -> dict[str, dict[str, float]]:
        """The scores a book reads: ``scores`` for a long-only book, plus
        ``shorts`` in universe order for a book that may short."""
        if not allow_short or not self.shorts:
            return {sid: dict(s) for sid, s in self.scores.items()}
        rank = {t: i for i, t in enumerate(self.universe)}
        out: dict[str, dict[str, float]] = {}
        for sid, longs in self.scores.items():
            merged = {**longs, **self.shorts.get(sid, {})}
            out[sid] = dict(sorted(merged.items(), key=lambda kv: rank.get(kv[0], len(rank))))
        return out

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
            shorts={**self.shorts, **other.shorts},
            universe=tuple(dict.fromkeys((*self.universe, *other.universe))),
        )


class Ranker:
    def __init__(
        self,
        registry: StrategyRegistry,
        lake: DuckDBLake,
        universe: Sequence[str],
        threshold: float = 0.0,
        status: Literal["active", "shadow"] = "active",
        workers: int = 1,
        min_parallel_estimates: int = 2000,
        allow_short: bool = False,
    ) -> None:
        """``workers`` > 1 scores the strategies that opt in with
        ``parallel_scoring`` in worker processes (``production.scoring``),
        once they need at least ``min_parallel_estimates`` estimates in all
        (below that a pool costs more than it saves). ``allow_short`` keeps
        the short scores of strategies that support shorts (module doc)."""
        self._allow_short = allow_short
        self._workers = workers
        self._min_parallel = min_parallel_estimates
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
        shorts: dict[str, dict[str, float]] = {}
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
        parallel = self._score_parallel(instances, as_of, asset_classes)
        for sid, strategy in instances.items():
            if sid in parallel:
                scores[sid] = parallel[sid]
            else:
                scores[sid], short = self._score_one(sid, strategy, as_of, asset_classes)
                if short:
                    shorts[sid] = short
        self.signals = SignalSet(
            as_of=as_of,
            scores=scores,
            instances=instances,
            shorts=shorts,
            universe=tuple(self._universe),
        )
        return self.signals.ranked()

    def _tickers_for(self, strategy: Strategy, asset_classes: Mapping[str, str]) -> list[str]:
        allowed = set(getattr(strategy, "applicable_asset_classes", ("equity",)))
        return [t for t in self._universe if asset_classes.get(t) in allowed]

    def _score_parallel(
        self, instances: Mapping[str, Strategy], as_of: date, asset_classes: Mapping[str, str]
    ) -> dict[str, dict[str, float]]:
        """Scores of the opted-in strategies from worker processes, or
        ``{}`` when the pool isn't worth it (or fails: then the serial path
        scores them)."""
        from stonks.production.scoring import parallel_safe, score_in_workers

        if self._workers <= 1:
            return {}
        safe = {
            sid: s for sid, s in instances.items() if parallel_safe(s) and not self._shorts_for(s)
        }
        tickers = {sid: self._tickers_for(s, asset_classes) for sid, s in safe.items()}
        if not safe or sum(len(t) for t in tickers.values()) < self._min_parallel:
            return {}
        try:
            return score_in_workers(
                self._lake, safe, tickers, as_of=as_of, threshold=self._threshold,
                workers=self._workers,
            )  # fmt: skip
        except Exception as exc:
            _log.warning("ranker.parallel_scoring.failed", error=str(exc),
                         error_type=type(exc).__name__)  # fmt: skip
            return {}

    def _score_one(
        self,
        strategy_id: str,
        strategy: Strategy,
        as_of: date,
        asset_classes: Mapping[str, str],
    ) -> tuple[dict[str, float], dict[str, float]]:
        """``(scores above the threshold, short scores below -threshold)``;
        the second is empty unless this strategy may short here."""
        allowed = set(getattr(strategy, "applicable_asset_classes", ("equity",)))
        shorting = self._shorts_for(strategy)
        out: dict[str, float] = {}
        shorts: dict[str, float] = {}
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
            if r is None:
                continue
            if r > self._threshold:
                out[ticker] = r
            elif shorting and r < -self._threshold:
                shorts[ticker] = r
        return out, shorts

    def _shorts_for(self, strategy: Strategy) -> bool:
        return self._allow_short and bool(getattr(strategy, "supports_short", False))


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

"""Parallel scoring for the signal phase (roadmap 15.5, S5; design section 5).

The signal phase scores every strategy once per tick for everyone, so on a
large catalog and universe it is the tick's CPU-bound part. Strategies that
opt in with ``parallel_scoring = True`` (their ``estimate_return`` keeps no
per-day state that ``decide`` reads) are scored in worker processes through
the one process pool, :func:`stonks.lab.parallel.run_tasks`:

- one task per strategy and chunk of up to :data:`CHUNK` tickers;
- workers read a read-only :class:`~stonks.lab.parallel.LakeSnapshot` built
  for the tick (the universe plus each strategy's ``reference_tickers``, up
  to ``as_of``), so the lake keeps its one writer and nothing changes under
  the run;
- results are merged back in universe order, so the scores equal the
  serial path's.

Strategies that don't opt in are scored in the tick's process, by the very
instance that later decides (``StrategyPool``).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

from stonks.core.interval import Interval
from stonks.core.protocols import Strategy
from stonks.logging import get_logger
from stonks.store.lake import DuckDBLake

_log = get_logger("stonks.production.scoring")

#: Tickers per worker task.
CHUNK = 250


def parallel_safe(strategy: Strategy) -> bool:
    return bool(getattr(strategy, "parallel_scoring", False))


def score_in_workers(
    lake: DuckDBLake,
    strategies: Mapping[str, Strategy],
    tickers: Mapping[str, Sequence[str]],
    *,
    as_of: date,
    threshold: float,
    workers: int,
) -> dict[str, dict[str, float]]:
    """``strategy_id -> ticker -> expected return`` (above ``threshold``)
    for each strategy over its ``tickers``, scored by ``workers`` processes.
    Raises what building the snapshot or the pool raises; a failing
    ``estimate_return`` only drops that ticker, as in the serial path."""
    from stonks.lab.parallel import LakeSnapshot, PortableStrategy, run_tasks

    universe = list(dict.fromkeys(t for ts in tickers.values() for t in ts))
    extra = [t for s in strategies.values() for t in getattr(s, "reference_tickers", ()) or ()]
    tasks: list[tuple[str, Any, list[str], float, date]] = []
    for sid, strategy in strategies.items():
        portable = PortableStrategy(strategy)
        names = list(tickers.get(sid, ()))
        for start in range(0, len(names), CHUNK):
            tasks.append((sid, portable, names[start : start + CHUNK], threshold, as_of))
    out: dict[str, dict[str, float]] = {sid: {} for sid in strategies}
    if not tasks:
        return out
    with LakeSnapshot.build(lake, [*universe, *extra], end=as_of) as snapshot:
        results = run_tasks(
            _score_chunk, tasks, setup=_open_snapshot, payload=str(snapshot.path),
            max_workers=workers,
        )  # fmt: skip
    for sid, scores in results:
        out[sid].update(scores)
    for sid in out:  # universe order, like the serial path
        order = {t: i for i, t in enumerate(tickers.get(sid, ()))}
        out[sid] = dict(sorted(out[sid].items(), key=lambda kv: order[kv[0]]))
    _log.info("scoring.parallel", strategies=len(strategies), tasks=len(tasks), workers=workers)
    return out


def _open_snapshot(path: str) -> DuckDBLake:
    return DuckDBLake(path, read_only=True)


def _score_chunk(
    lake: DuckDBLake, task: tuple[str, Any, list[str], float, date]
) -> tuple[str, dict[str, float]]:
    from stonks.strategies._common import decision_interval

    sid, portable, names, threshold, as_of = task
    strategy = portable.strategy
    out: dict[str, float] = {}
    with decision_interval(Interval.DAY_1):
        for ticker in names:
            try:
                r = strategy.estimate_return(ticker, as_of, lake)
            except Exception as exc:
                _log.warning("ranker.estimate_return.failed", strategy_id=sid, ticker=ticker,
                             error=str(exc))  # fmt: skip
                continue
            if r is None or r <= threshold:
                continue
            out[ticker] = r
    return sid, out

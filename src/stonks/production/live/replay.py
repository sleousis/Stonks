"""A replay of the last sessions before a book starts, restarts or moves up
a stage (roadmap 23.15).

Each strategy is asked for its view on every ticker of the universe it can
trade, on each of the last ``sessions`` sessions the lake has bars for, read
point in time (P12). The replay only asks whether the strategy works:

- it **blocks** when a strategy made no decision at all (every answer was
  "no view"), when every call raised, or when it did not load;
- a few errors, or views that would have lost money, never block. The
  replay never reads prices after a session and never looks at profit or
  loss, so it cannot turn into "only start after a good week".

``passed`` is ``None`` (shown, not blocking) when there is nothing to
replay: no universe, no strategy, no sessions in the lake, or the replay
switched off (``[production.live.replay] enabled = false``).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import pandas as pd

from stonks.core.interval import Interval
from stonks.logging import get_logger
from stonks.production.live.settings import ReplaySettings
from stonks.store.pit import PitSession

_log = get_logger("stonks.production.live.replay")


@dataclass(frozen=True)
class StrategyReplay:
    strategy_id: str
    sessions: int = 0
    #: ``estimate_return`` calls made (tickers it can trade x sessions).
    calls: int = 0
    #: Answers with a view (any number, a negative one too).
    decisions: int = 0
    errors: int = 0
    load_error: str | None = None
    #: The first error seen, for the person reading the report.
    first_error: str | None = None

    @property
    def blocker(self) -> str | None:
        if self.load_error is not None:
            return f"{self.strategy_id} did not load: {self.load_error}"
        if self.calls and self.errors == self.calls:
            return (
                f"{self.strategy_id}: every call failed in the last {self.sessions} sessions"
                f" ({self.first_error})"
            )
        if self.decisions == 0:
            return f"{self.strategy_id}: no decision in the last {self.sessions} sessions"
        return None


@dataclass(frozen=True)
class ReplayReport:
    sessions: tuple[date, ...] = ()
    strategies: tuple[StrategyReplay, ...] = ()
    #: ``None``: nothing to replay (not blocking).
    passed: bool | None = None
    detail: str = ""
    universe_size: int = 0
    notes: tuple[str, ...] = field(default=())

    def blockers(self) -> list[str]:
        return [b for s in self.strategies if (b := s.blocker) is not None]

    def as_dict(self) -> dict[str, Any]:
        return {
            "sessions": [d.isoformat() for d in self.sessions],
            "passed": self.passed,
            "detail": self.detail,
            "universe_size": self.universe_size,
            "strategies": [
                {
                    "strategy_id": s.strategy_id,
                    "sessions": s.sessions,
                    "calls": s.calls,
                    "decisions": s.decisions,
                    "errors": s.errors,
                    "load_error": s.load_error,
                    "blocker": s.blocker,
                }
                for s in self.strategies
            ],
        }


def recent_sessions(lake: Any, universe: Sequence[str], as_of: date, n: int) -> list[date]:
    """The last ``n`` days on or before ``as_of`` with a daily bar for any
    ticker of ``universe``, oldest first."""
    df = lake.sql(
        "SELECT DISTINCT date FROM prices WHERE ticker = ANY(?) AND date <= ?"
        " ORDER BY date DESC LIMIT ?",
        [list(universe), as_of, n],
    )
    return sorted(date.fromisoformat(pd.Timestamp(d).strftime("%Y-%m-%d")) for d in df["date"])


def replay_strategies(
    lake: Any,
    loaders: Mapping[str, Callable[[], Any]],
    universe: Sequence[str],
    as_of: date,
    *,
    sessions: int,
) -> ReplayReport:
    """Replay ``loaders`` (strategy id to a loader) over the last
    ``sessions`` sessions on or before ``as_of``."""
    tickers = list(dict.fromkeys(universe))
    if not loaders or not tickers:
        what = "no strategy" if not loaders else "no universe"
        return ReplayReport(passed=None, detail=f"nothing to replay: {what}")
    days = recent_sessions(lake, tickers, as_of, sessions)
    if not days:
        return ReplayReport(passed=None, detail="nothing to replay: no bars in the lake")
    classes = lake.get_asset_classes(tickers)
    pit = PitSession(lake)
    views = {d: pit.at(d, decision_interval=Interval.DAY_1) for d in days}
    results: list[StrategyReplay] = []
    for sid, load in loaders.items():
        try:
            strategy = load()
        except Exception as exc:
            results.append(StrategyReplay(sid, load_error=str(exc) or type(exc).__name__))
            continue
        allowed = set(getattr(strategy, "applicable_asset_classes", ("equity",)))
        mine = [t for t in tickers if classes.get(t) in allowed]
        calls = decisions = errors = 0
        first_error: str | None = None
        for day in days:
            for ticker in mine:
                calls += 1
                try:
                    view = strategy.estimate_return(ticker, day, views[day])
                except Exception as exc:
                    errors += 1
                    first_error = first_error or f"{ticker} on {day}: {exc}"
                    continue
                if view is not None:
                    decisions += 1
        results.append(
            StrategyReplay(sid, len(days), calls, decisions, errors, first_error=first_error)
        )
    report = ReplayReport(
        sessions=tuple(days),
        strategies=tuple(results),
        universe_size=len(tickers),
    )
    blockers = report.blockers()
    passed = not blockers
    detail = (
        f"{len(results)} strategies over {len(days)} sessions decided"
        if passed
        else "; ".join(blockers)
    )
    _log.info("live.replay", passed=passed, sessions=len(days), blockers=blockers)
    return ReplayReport(
        sessions=report.sessions,
        strategies=report.strategies,
        passed=passed,
        detail=detail,
        universe_size=len(tickers),
    )


def replay_check(
    lake: Any,
    registry: Any,
    strategy_ids: Sequence[str],
    universe: Sequence[str],
    as_of: date,
    settings: ReplaySettings,
) -> ReplayReport:
    """The replay for ``strategy_ids`` loaded from ``registry``."""
    if not settings.enabled:
        return ReplayReport(passed=None, detail="replay is off ([production.live.replay])")
    loaders: dict[str, Callable[[], Any]] = {
        sid: (lambda sid=sid: registry.load(sid)) for sid in dict.fromkeys(strategy_ids)
    }
    return replay_strategies(lake, loaders, universe, as_of, sessions=settings.sessions)

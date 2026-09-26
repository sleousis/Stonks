"""Event study against baseline drift (BL-34, after Grimes; principles P5, P37).

Does entering when the strategy says so beat simply being in the same
tickers over the same period? And does that hold in every asset class the
strategy trades? (A 50-day breakout has a positive edge in futures and a
negative one in single stocks.)

- **Events** are the bars where a ticker newly enters the strategy's picks:
  its ``estimate_return`` is above the engine's threshold (0) at bar ``t``
  and was not at the ticker's previous bar. A pick on the first bar of the
  window has no prior state and is not an event.
- **Returns** after an event are forward returns from the next open,
  ``O[t+1+h] / O[t+1] - 1`` on split- and dividend-adjusted opens, never
  reading past the window end (``lab.signal_eval``): the same fill
  convention as the backtest engine, so no look-ahead.
- **Baseline drift** is the mean of the same forward return over every
  bar of the tickers that produced events, in the same window.
- **Excess** is the event mean minus the baseline. Its uncertainty comes
  from a stationary block bootstrap of the time-ordered events
  (``n_boot`` draws, seeded). Forward returns over ``h`` bars overlap, so
  a block covers the events expected inside one ``h``-bar span,
  ``h * n_events / n_bars`` (at least one event; see
  :func:`event_block_length`): a percentile ``1 - alpha`` interval and the
  one-sided p-value ``(#{excess* <= 0} + 1) / (n_boot + 1)``.
- **Holding horizon**: ``holding_bars`` when set, else the average bars
  held of the strategy's backtest trade ledger over the window, else 20.
  It is always among the horizons evaluated.

The test runs per asset class (``instruments.asset_class``; missing means
equity) and pooled (``"all"``). Every asset class with at least
``min_events`` events must show a positive excess at the holding horizon
with ``p <= alpha``; when no single class has that many, the pooled
events are judged instead. Fewer than ``min_events`` events in total fails
with a note. Only events with a forward return at the holding horizon count
toward ``min_events`` (RS-27): an entry in the last ``h`` bars has none. Scores are computed per ticker on the lab process pool
(``lab.parallel`` via ``lab.signal_eval``); the bootstrap is seeded per
group and horizon, so the report does not depend on ``max_workers``.
"""

from __future__ import annotations

import dataclasses
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, field_validator

from stonks.core.protocols import Strategy, SurvivalReport
from stonks.lab.dataset import scoring_window
from stonks.lab.signal_eval import (
    forward_returns_from_bars,
    sampled_timeline,
    scores_on,
    window_bars,
)
from stonks.logging import get_logger
from stonks.stats.bootstrap import stationary_bootstrap_indices

_log = get_logger("stonks.lab.survival.event_study")

DEFAULT_EVENT_HORIZONS: tuple[int, ...] = (1, 5, 10, 20, 60)
#: Holding horizon when neither the option nor the trade ledger gives one.
DEFAULT_HOLDING_BARS = 20
#: The engine's pick threshold (``BacktestConfig.threshold``).
PICK_THRESHOLD = 0.0


def entry_events(picks: np.ndarray) -> np.ndarray:
    """Indices where ``picks`` turns True after a False (index 0 never)."""
    p = np.asarray(picks, dtype=bool)
    if p.size < 2:
        return np.empty(0, dtype=int)
    return np.nonzero(p[1:] & ~p[:-1])[0] + 1


@dataclass(frozen=True)
class HorizonEvents:
    horizon: int
    n_events: int
    event_mean: float
    baseline_mean: float
    excess: float
    ci_low: float
    ci_high: float
    p_value: float


@dataclass(frozen=True)
class EventGroup:
    #: ``"all"`` or an asset class.
    asset_class: str
    n_tickers: int
    n_events: int
    horizons: list[HorizonEvents]

    def at(self, horizon: int) -> HorizonEvents:
        return next(h for h in self.horizons if h.horizon == horizon)


@dataclass(frozen=True)
class EventStudyResult:
    strategy_id: str
    window: tuple[str, str]
    holding_bars: int
    #: Where ``holding_bars`` came from: ``option``, ``ledger`` or ``default``.
    holding_source: str
    alpha: float
    n_boot: int
    seed: int
    n_events: int
    groups: list[EventGroup] = field(default_factory=list)

    def group(self, name: str) -> EventGroup | None:
        return next((g for g in self.groups if g.asset_class == name), None)

    def to_dict(self) -> dict[str, Any]:
        """Plain JSON-ready data; NaN becomes ``None``."""
        from stonks.lab.signal_eval import _clean

        return _clean(dataclasses.asdict(self))

    def metrics(self) -> dict[str, float]:
        out = {"n_events": float(self.n_events), "holding_bars": float(self.holding_bars)}
        for g in self.groups:
            prefix = "" if g.asset_class == "all" else f"{g.asset_class}."
            if prefix:
                out[f"{prefix}n_events"] = float(g.n_events)
            for h in g.horizons:
                out[f"{prefix}excess_h{h.horizon}"] = h.excess
                out[f"{prefix}p_value_h{h.horizon}"] = h.p_value
                out[f"{prefix}ci_low_h{h.horizon}"] = h.ci_low
                out[f"{prefix}ci_high_h{h.horizon}"] = h.ci_high
        return out

    def summary(self) -> str:
        lines = [
            f"event study {self.strategy_id}: {self.n_events} events, holding "
            f"{self.holding_bars} bars ({self.holding_source})"
        ]
        for g in self.groups:
            h = g.at(self.holding_bars)
            lines.append(
                f"  {g.asset_class:<10} events {g.n_events:>5}  excess {h.excess:+.4%}  "
                f"CI [{h.ci_low:+.4%}, {h.ci_high:+.4%}]  p {h.p_value:.3f}"
            )
        return "\n".join(lines)


def _asset_classes(dataset: Any, tickers: Sequence[str]) -> dict[str, str]:
    frame = dataset.lake.sql(
        "SELECT id, asset_class FROM instruments WHERE id = ANY(?)", [list(tickers)]
    )
    known = {str(r.id): str(r.asset_class) for r in frame.itertuples(index=False) if r.asset_class}
    return {t: known.get(t, "equity") for t in tickers}


def _holding_bars(
    strategy: Strategy, dataset: Any, window: tuple[date, date], holding: int | None
) -> tuple[int, str]:
    if holding is not None:
        return int(holding), "option"
    from stonks.lab.backtesting import run_backtest

    try:
        avg = float(run_backtest(strategy, dataset, window).trade_stats.avg_bars_held)
    except Exception as exc:  # the ledger is a convenience; never fail on it
        _log.warning("event_study.ledger_failed", error=str(exc))
        avg = 0.0
    if avg >= 1:
        return int(round(avg)), "ledger"
    return DEFAULT_HOLDING_BARS, "default"


def event_block_length(horizon: int, *, n_events: int, n_bars: int) -> float:
    """Mean bootstrap block, in events: how many events fall inside one
    ``horizon``-bar span on average (their forward returns overlap),
    clipped to ``[1, n_events]``."""
    if n_events < 1 or n_bars < 1:
        return 1.0
    return float(min(max(horizon * n_events / n_bars, 1.0), n_events))


def _horizon_stats(
    events: np.ndarray,
    baseline: np.ndarray,
    horizon: int,
    *,
    n_bars: int,
    n_boot: int,
    alpha: float,
    rng: np.random.Generator,
) -> HorizonEvents:
    ev = events[np.isfinite(events)]
    base = baseline[np.isfinite(baseline)]
    base_mean = float(base.mean()) if base.size else math.nan
    if ev.size < 2 or not math.isfinite(base_mean):
        mean = float(ev.mean()) if ev.size else math.nan
        return HorizonEvents(
            horizon, int(ev.size), mean, base_mean, mean - base_mean, math.nan, math.nan, 1.0
        )
    block = event_block_length(horizon, n_events=int(ev.size), n_bars=n_bars)
    idx = stationary_bootstrap_indices(ev.size, block, n_boot, rng)
    boot = ev[idx].mean(axis=1) - base_mean
    lo, hi = np.quantile(boot, [alpha / 2, 1 - alpha / 2])
    mean = float(ev.mean())
    return HorizonEvents(
        horizon=horizon,
        n_events=int(ev.size),
        event_mean=mean,
        baseline_mean=base_mean,
        excess=mean - base_mean,
        ci_low=float(lo),
        ci_high=float(hi),
        p_value=float((np.sum(boot <= 0) + 1) / (n_boot + 1)),
    )


def event_study(
    strategy: Strategy,
    dataset: Any,
    window: tuple[date, date],
    *,
    horizons: Sequence[int] = DEFAULT_EVENT_HORIZONS,
    holding_bars: int | None = None,
    n_boot: int = 1000,
    alpha: float = 0.05,
    seed: int = 17,
    max_workers: int | None = None,
) -> EventStudyResult:
    """Event returns against baseline drift, pooled and per asset class
    (see module doc)."""
    holding, source = _holding_bars(strategy, dataset, window, holding_bars)
    hs = tuple(sorted({int(h) for h in horizons} | {holding}))
    bars = window_bars(dataset, window)
    timeline = sampled_timeline(bars, 1)
    tickers = list(bars)
    scores = scores_on(strategy, dataset, bars, timeline, max_workers=max_workers)
    fwd = forward_returns_from_bars(bars, hs, timeline, tickers)
    classes = _asset_classes(dataset, tickers)

    # per ticker, on its own bars: event rows (timeline positions), all rows
    event_rows: dict[str, np.ndarray] = {}
    own_rows: dict[str, np.ndarray] = {}
    for ticker, tb in bars.items():
        rows = timeline.get_indexer(tb.timestamps)
        picks = scores[ticker].to_numpy(float)[rows] > PICK_THRESHOLD  # NaN compares False
        own_rows[ticker] = rows
        event_rows[ticker] = rows[entry_events(picks)]
    n_events = int(sum(len(r) for r in event_rows.values()))
    _log.info("event_study.events", strategy=getattr(strategy, "id", "?"), events=n_events)

    names = ["all", *sorted(set(classes.values()))]
    groups: list[EventGroup] = []
    for g_index, name in enumerate(names):
        members = [t for t in tickers if name == "all" or classes[t] == name]
        with_events = [t for t in members if len(event_rows[t])] or members
        g_events = int(sum(len(event_rows[t]) for t in members))
        stats: list[HorizonEvents] = []
        for h in hs:
            frame = fwd[h]
            # events in time order (then universe order) for the block bootstrap
            pairs = sorted((int(r), k) for k, t in enumerate(members) for r in event_rows[t])
            values = np.array(
                [frame.iat[r, frame.columns.get_loc(members[k])] for r, k in pairs], dtype=float
            )
            base = np.concatenate([frame[t].to_numpy(float)[own_rows[t]] for t in with_events])
            rng = np.random.default_rng([seed, g_index, h])
            stats.append(
                _horizon_stats(
                    values, base, h, n_bars=len(timeline), n_boot=n_boot, alpha=alpha, rng=rng
                )
            )
        groups.append(EventGroup(name, len(members), g_events, stats))
    return EventStudyResult(
        strategy_id=str(getattr(strategy, "id", type(strategy).__name__)),
        window=(str(window[0]), str(window[1])),
        holding_bars=holding,
        holding_source=source,
        alpha=alpha,
        n_boot=n_boot,
        seed=seed,
        n_events=n_events,
        groups=groups,
    )


class EventStudyOptions(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    horizons: tuple[int, ...] = DEFAULT_EVENT_HORIZONS
    min_events: int = Field(default=100, ge=30, le=500)
    alpha: float = Field(default=0.05, gt=0.0, le=0.2)
    n_boot: int = Field(default=1000, ge=100)
    #: Typical holding horizon in bars; ``None`` reads the trade ledger.
    holding_bars: int | None = Field(default=None, ge=1)
    window: Literal["val", "full"] = "val"
    seed: int = 17
    #: Worker processes; ``None`` means ``lab.parallel.default_max_workers()``.
    max_workers: int | None = Field(default=None, ge=1)

    @field_validator("horizons")
    @classmethod
    def _horizons(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if not value or min(value) < 1:
            raise ValueError("horizons must be a non-empty list of bar counts >= 1")
        return tuple(sorted(set(value)))


class EventStudyTest:
    id = "event_study"
    Options = EventStudyOptions

    def __init__(self, options: EventStudyOptions | None = None, **overrides: Any) -> None:
        base = options or EventStudyOptions()
        self.options = (
            EventStudyOptions.model_validate({**base.model_dump(), **overrides})
            if overrides
            else base
        )

    @classmethod
    def build(cls, options: EventStudyOptions) -> EventStudyTest:
        return cls(options)

    def run(self, strategy: Strategy, context: Any) -> SurvivalReport:
        opts = self.options
        window = scoring_window(context, strategy, opts.window)
        result = event_study(
            strategy,
            context,
            window,
            horizons=opts.horizons,
            holding_bars=opts.holding_bars,
            n_boot=opts.n_boot,
            alpha=opts.alpha,
            seed=opts.seed,
            max_workers=opts.max_workers,
        )
        metrics = result.metrics()
        span = f"window {window[0]}..{window[1]}; holding {result.holding_bars} bars"
        h = result.holding_bars
        # RS-27: only events with a forward return at the holding horizon
        # count (an entry in the last h bars has none)
        pooled = result.group("all")
        n_scored = pooled.at(h).n_events if pooled is not None else 0
        metrics["n_events_scored"] = float(n_scored)
        if n_scored < opts.min_events:
            note = (
                f"fewer than {opts.min_events} events with a {h}-bar forward return "
                f"({n_scored} of {result.n_events} entries); {span}"
            )
            return SurvivalReport(self.id, False, metrics, note)
        classes = [g for g in result.groups if g.asset_class != "all"]
        judged = [g for g in classes if g.at(h).n_events >= opts.min_events]
        thin = [g.asset_class for g in classes if g.at(h).n_events < opts.min_events]
        if not judged:
            judged = [g for g in result.groups if g.asset_class == "all"]
        failed = [g for g in judged if not (g.at(h).excess > 0 and g.at(h).p_value <= opts.alpha)]
        parts = [
            f"{g.asset_class}: excess {g.at(h).excess:+.4%} p={g.at(h).p_value:.3f}" for g in judged
        ]
        head = (
            "no significant edge over baseline drift in " + ", ".join(g.asset_class for g in failed)
            if failed
            else "entries beat baseline drift"
        )
        notes = [head, *parts, span]
        if thin:
            notes.append(f"too few events to judge: {', '.join(thin)}")
        return SurvivalReport(self.id, not failed, metrics, "; ".join(notes))

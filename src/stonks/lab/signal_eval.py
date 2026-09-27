"""Signal evaluation: does a strategy's ``estimate_return`` rank future
returns? (BL-33, principle P5.)

Any catalogued strategy's ``estimate_return`` is treated as a signal and
scored across its universe, before sizing and costs mix into a backtest:

- **Scores** ``s[t, i]``: ``estimate_return(ticker_i, t, lake)`` at bar
  ``t`` of the dataset interval, every ``every_bars`` bars of the union
  timeline, and only where the ticker has a bar at ``t``. ``None`` (or a
  non-finite value) is a missing score. A strategy that returns ``None``
  below its entry threshold (``Momentum``) is scored on the names it would
  consider only, so its IC describes the ranking among those.
- **Forward returns** ``r_h[t, i] = O[t+1+h] / O[t+1] - 1`` on the
  ticker's own bars, with ``O`` the split- and dividend-adjusted open. The
  score at ``t`` sees data up to ``t``'s close; the backtest engine fills
  at the next open, so returns start there (a score that peeks at the gap
  into ``t+1`` earns nothing). Returns never read a bar after the window
  end: a date without ``1 + h`` later bars in the window has no return.
- **IC** per date: the Spearman rank correlation of scores and forward
  returns across tickers (at least ``min_names`` names with both). Per
  horizon: mean IC, IC standard deviation, ICIR (mean / std), the share of
  positive ICs and a Newey-West t-stat. Forward returns over ``h`` bars
  sampled every ``k`` bars overlap for ``ceil(h / k) - 1`` samples, so the
  HAC standard error uses that many lags (``h - 1`` at ``k = 1``); the iid
  standard error is reported next to it.
- **Decay**: the mean IC at each horizon (the list of horizons is it).
- **Quantile spread**: per date the names are split into ``n_quantiles``
  equal-count buckets by score; the mean forward return of each bucket,
  averaged over dates, and the top-minus-bottom spread (with its HAC t).
- **Turnover**: ``1 -`` the mean rank autocorrelation of scores between
  consecutive sampled dates, and the mean share of the top bucket that is
  replaced from one sampled date to the next.

Universes with fewer than :data:`MIN_UNIVERSE` tickers are ``n/a``: a
rank correlation over a handful of names is noise.

The mean IC at the strategy's horizon (``label_horizon_bars`` when it
declares one, the nearest evaluated horizon; else 21 bars when evaluated,
else the longest) is ``ic_estimate``, the ``IC_s`` of BL-08's ``alpha``
normalisation.

Scoring is the expensive part (one ``estimate_return`` per ticker and
date); it fans out per ticker through ``lab.parallel.run_tasks``, each
worker reading its own read-only snapshot of the lake. Nothing here is
random, so the result does not depend on ``max_workers``.

Command line (writes nothing to the lake; opens it read-only)::

    uv run python -m stonks.lab.signal_eval --strategy momentum \
        --tickers AAPL.US,MSFT.US,... --start 2024-01-01 --end 2025-01-01 \
        --horizons 1,5,21 --every-bars 5 --events --html signals.html
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import math
import pickle
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from stonks.core.interval import Interval
from stonks.core.protocols import Strategy
from stonks.core.timeutil import day_end, day_start
from stonks.features.price_adjustment import SeriesAdjustment
from stonks.lab.parallel import (
    DatasetSpec,
    PortableStrategy,
    dataset_snapshot,
    planned_workers,
    run_tasks,
)
from stonks.logging import get_logger
from stonks.stats.hac import newey_west_se
from stonks.store.corporate_actions import LakeCorporateActions
from stonks.strategies._common import decision_interval
from stonks.strategies.base import strategy_data_tickers

_log = get_logger("stonks.lab.signal_eval")

__all__ = [
    "DEFAULT_HORIZONS",
    "MIN_UNIVERSE",
    "HorizonIC",
    "SignalICResult",
    "TickerBars",
    "forward_returns",
    "ic_by_date",
    "map_over_tickers",
    "score_panel",
    "signal_ic",
    "window_bars",
]

DEFAULT_HORIZONS: tuple[int, ...] = (1, 5, 21)
#: Fewer tickers than this and the IC analysis is ``n/a``.
MIN_UNIVERSE = 10
#: Horizon ``ic_estimate`` uses when the strategy declares no label horizon.
DEFAULT_IC_HORIZON = 21


# ---- bars and forward returns ---------------------------------------------


@dataclass(frozen=True)
class TickerBars:
    """One ticker's bars inside a window: timestamps and adjusted opens."""

    timestamps: pd.DatetimeIndex
    opens: np.ndarray


def window_bars(dataset: Any, window: tuple[date, date]) -> dict[str, TickerBars]:
    """Per ticker of ``dataset.universe``: its dataset-interval bars in
    ``window`` with opens back-adjusted for splits and dividends (ratios
    of adjusted opens are total returns). Tickers without bars are left
    out."""
    interval = getattr(dataset, "interval", Interval.DAY_1)
    universe = list(dataset.universe)
    frame = dataset.lake.sql(
        """
        SELECT ticker, timestamp, open, close, adj_close
          FROM bars
         WHERE ticker = ANY(?) AND interval = ? AND timestamp BETWEEN ? AND ?
         ORDER BY ticker, timestamp
        """,
        [universe, interval.code, day_start(window[0]), day_end(window[1])],
    )
    actions = LakeCorporateActions(dataset.lake).load(universe)
    out: dict[str, TickerBars] = {}
    for ticker, bars in frame.groupby("ticker", sort=False):
        bars = bars.reset_index(drop=True)
        adjusted = SeriesAdjustment.build(bars, actions.for_ticker(str(ticker))).apply(
            bars, 0, len(bars)
        )
        opens = adjusted["open"].to_numpy(dtype=float)
        opens = np.where(opens > 0, opens, np.nan)
        out[str(ticker)] = TickerBars(pd.DatetimeIndex(pd.to_datetime(bars["timestamp"])), opens)
    return dict(sorted(out.items(), key=lambda kv: universe.index(kv[0])))


def _positions(bars: TickerBars, timeline: pd.DatetimeIndex) -> np.ndarray:
    """Index of each ``timeline`` stamp in the ticker's bars, -1 where the
    ticker has no bar at that stamp."""
    pos = bars.timestamps.get_indexer(timeline)
    return np.asarray(pos, dtype=int)


def forward_returns_from_bars(
    bars: Mapping[str, TickerBars],
    horizons: Sequence[int],
    timeline: pd.DatetimeIndex,
    columns: Sequence[str],
) -> dict[int, pd.DataFrame]:
    """``{h: frame}`` of ``O[t+1+h] / O[t+1] - 1`` (see module doc), one
    row per ``timeline`` stamp and one column per ticker in ``columns``."""
    out = {h: np.full((len(timeline), len(columns)), np.nan) for h in horizons}
    for j, ticker in enumerate(columns):
        tb = bars.get(ticker)
        if tb is None:
            continue
        pos = _positions(tb, timeline)
        n = len(tb.opens)
        for h in horizons:
            ok = (pos >= 0) & (pos + 1 + h < n)
            entry = np.full(len(pos), np.nan)
            exit_ = np.full(len(pos), np.nan)
            entry[ok] = tb.opens[pos[ok] + 1]
            exit_[ok] = tb.opens[pos[ok] + 1 + h]
            out[h][:, j] = exit_ / entry - 1.0
    return {h: pd.DataFrame(v, index=timeline, columns=list(columns)) for h, v in out.items()}


def forward_returns(
    dataset: Any,
    window: tuple[date, date],
    horizons: Sequence[int],
    timeline: Sequence[Any],
) -> dict[int, pd.DataFrame]:
    """Forward returns from the next open for every stamp of ``timeline``
    and ticker of ``dataset.universe``, reading only bars in ``window``."""
    index = pd.DatetimeIndex(pd.to_datetime(list(timeline)))
    return forward_returns_from_bars(
        window_bars(dataset, window), horizons, index, list(dataset.universe)
    )


def sampled_timeline(bars: Mapping[str, TickerBars], every_bars: int) -> pd.DatetimeIndex:
    """Every ``every_bars``-th stamp of the union of the tickers' bars."""
    if not bars:
        return pd.DatetimeIndex([])
    union = bars[next(iter(bars))].timestamps
    for tb in list(bars.values())[1:]:
        union = union.union(tb.timestamps)
    return union.sort_values()[::every_bars]


# ---- scoring over the process pool ------------------------------------------


@dataclass(frozen=True)
class _StrategyState:
    #: ``PortableStrategy`` state: the class and its saved files.
    saved: tuple[type, dict[str, bytes]] | None
    strategy: Any
    dataset: Any


def _open_state(state: _StrategyState) -> _StrategyState:
    dataset = state.dataset.open() if isinstance(state.dataset, DatasetSpec) else state.dataset
    handle = PortableStrategy.__new__(PortableStrategy)
    handle.__setstate__({"portable": state.saved})
    return dataclasses.replace(state, strategy=handle.strategy, dataset=dataset)


def map_over_tickers[T, R](
    fn: Callable[[Strategy, Any, T], R],
    strategy: Strategy,
    dataset: Any,
    tasks: Sequence[T],
    *,
    max_workers: int | None = None,
    log_prefix: str = "signal_eval",
) -> list[R]:
    """``[fn(strategy, dataset, task) for task in tasks]`` over the lab
    pool. Workers get a reloaded copy of ``strategy`` (``save``/``load``)
    and the dataset on a read-only lake snapshot; the in-process path uses
    ``strategy`` and ``dataset`` themselves. ``fn`` must be a module-level
    function. Work that cannot be pickled runs in-process."""
    tasks = list(tasks)
    workers = planned_workers(len(tasks), max_workers=max_workers)
    if workers > 1:
        problem = _unpicklable(strategy, dataset, fn, tasks[0])
        if problem is None:
            saved = PortableStrategy(strategy).__getstate__()["portable"]
            with dataset_snapshot(dataset, strategy_data_tickers(strategy)) as shipped:
                return run_tasks(
                    _run_fn,
                    [(fn, t) for t in tasks],
                    setup=_open_state,
                    payload=_StrategyState(saved, None, shipped),
                    max_workers=workers,
                )
        _log.warning(f"{log_prefix}.parallel.unavailable", workers=workers, reason=problem)
    state = _StrategyState(None, strategy, dataset)
    return run_tasks(_run_fn, [(fn, t) for t in tasks], payload=state, max_workers=1)


def _run_fn(state: _StrategyState, task: tuple[Callable[..., Any], Any]) -> Any:
    fn, arg = task
    return fn(state.strategy, state.dataset, arg)


def _unpicklable(strategy: Any, dataset: Any, *objects: Any) -> str | None:
    try:
        pickle.dumps(PortableStrategy(strategy))
        if dataclasses.is_dataclass(dataset) and hasattr(dataset, "lake"):
            pickle.dumps(dataclasses.replace(dataset, lake=None))
        pickle.dumps(objects)
    except Exception as exc:  # pickle raises many types
        return f"{type(exc).__name__}: {exc}"
    return None


def _score_ticker(strategy: Strategy, dataset: Any, task: tuple[str, list[datetime]]) -> np.ndarray:
    ticker, stamps = task
    out = np.full(len(stamps), np.nan)
    # each score decides on a bar of the dataset's interval, so it sees a
    # daily close only after that day ends (RS-03, BE-21)
    with decision_interval(getattr(dataset, "interval", None)):
        for k, as_of in enumerate(stamps):
            value = strategy.estimate_return(ticker, as_of, dataset.lake)
            if value is not None:
                v = float(value)
                out[k] = v if math.isfinite(v) else np.nan
    return out


def scores_on(
    strategy: Strategy,
    dataset: Any,
    bars: Mapping[str, TickerBars],
    timeline: pd.DatetimeIndex,
    *,
    max_workers: int | None = None,
) -> pd.DataFrame:
    """Scores for every ``timeline`` stamp (rows) and universe ticker
    (columns), asked only where the ticker has a bar at that stamp."""
    columns = list(dataset.universe)
    tasks: list[tuple[str, list[datetime]]] = []
    where: list[tuple[int, np.ndarray]] = []
    for j, ticker in enumerate(columns):
        tb = bars.get(ticker)
        if tb is None:
            continue
        rows = np.nonzero(_positions(tb, timeline) >= 0)[0]
        if rows.size:
            tasks.append((ticker, [timeline[r].to_pydatetime() for r in rows]))
            where.append((j, rows))
    values = np.full((len(timeline), len(columns)), np.nan)
    if tasks:
        results = map_over_tickers(_score_ticker, strategy, dataset, tasks, max_workers=max_workers)
        for (j, rows), scores in zip(where, results, strict=True):
            values[rows, j] = scores
    return pd.DataFrame(values, index=timeline, columns=columns)


def score_panel(
    strategy: Strategy,
    dataset: Any,
    window: tuple[date, date],
    *,
    every_bars: int = 1,
    max_workers: int | None = None,
) -> pd.DataFrame:
    """Scores (dates x tickers) every ``every_bars`` bars of ``window``."""
    if every_bars < 1:
        raise ValueError(f"every_bars must be >= 1, got {every_bars}")
    bars = window_bars(dataset, window)
    return scores_on(
        strategy, dataset, bars, sampled_timeline(bars, every_bars), max_workers=max_workers
    )


# ---- statistics -------------------------------------------------------------


def _spearman_rows(a: np.ndarray, b: np.ndarray, min_names: int) -> np.ndarray:
    """Row-wise Spearman correlation of two ``T x N`` arrays over the
    columns finite in both; NaN with fewer than ``min_names`` or no spread."""
    out = np.full(a.shape[0], np.nan)
    for t in range(a.shape[0]):
        ok = np.isfinite(a[t]) & np.isfinite(b[t])
        if ok.sum() < min_names:
            continue
        ra = pd.Series(a[t, ok]).rank().to_numpy()
        rb = pd.Series(b[t, ok]).rank().to_numpy()
        if ra.std() == 0 or rb.std() == 0:
            continue
        out[t] = float(np.corrcoef(ra, rb)[0, 1])
    return out


def ic_by_date(scores: pd.DataFrame, fwd: pd.DataFrame, *, min_names: int = 5) -> pd.Series:
    """Spearman IC per date (row) of ``scores`` against ``fwd``."""
    return pd.Series(
        _spearman_rows(scores.to_numpy(float), fwd.to_numpy(float), min_names), index=scores.index
    )


def _mean_se(x: np.ndarray, lags: int) -> tuple[float, float, float, float]:
    """``(mean, std ddof=1, iid se, HAC se)`` of the finite values.

    The HAC se keeps the dates of missing values (RS-28): the lag-``l``
    autocovariance sums only pairs of observed values that are ``l`` dates
    apart, divided by the number of observed dates (Parzen's amplitude-
    modulated series). Dropping the gaps first would pair dates further
    apart than ``l`` and break the ``ceil(h/k) - 1`` overlap the lags model.
    With no gaps this is :func:`~stonks.stats.hac.newey_west_se`."""
    ok = np.isfinite(x)
    v = x[ok]
    if v.size < 2:
        return (float(v.mean()) if v.size else math.nan, math.nan, math.nan, math.nan)
    return (
        float(v.mean()),
        float(v.std(ddof=1)),
        newey_west_se(v, 0),
        _gap_aware_hac_se(x, lags) if not ok.all() else newey_west_se(v, lags),
    )


def _gap_aware_hac_se(x: np.ndarray, lags: int) -> float:
    ok = np.isfinite(x)
    n = int(ok.sum())
    d = np.where(ok, x - x[ok].mean(), 0.0)
    n_lags = min(max(int(lags), 0), len(x) - 1)
    s = float(d @ d) / n
    for lag in range(1, n_lags + 1):
        s += 2 * (1 - lag / (n_lags + 1)) * float(d[lag:] @ d[:-lag]) / n
    return math.sqrt(max(s, 0.0) / n)


def _ratio(a: float, b: float) -> float:
    """``a / b``; a non-zero ``a`` over a zero spread is infinitely sure."""
    if not (math.isfinite(a) and math.isfinite(b)) or b < 0:
        return math.nan
    if b == 0:
        return math.copysign(math.inf, a) if a != 0 else math.nan
    return a / b


def _quantile_returns(
    scores: np.ndarray, fwd: np.ndarray, n_quantiles: int, min_names: int
) -> np.ndarray:
    """``T x n_quantiles`` mean forward return per score bucket per date
    (NaN rows where too few names have both)."""
    out = np.full((scores.shape[0], n_quantiles), np.nan)
    need = max(n_quantiles, min_names)
    for t in range(scores.shape[0]):
        ok = np.isfinite(scores[t]) & np.isfinite(fwd[t])
        if ok.sum() < need:
            continue
        buckets = _buckets(scores[t, ok], n_quantiles)
        r = fwd[t, ok]
        out[t] = [r[buckets == q].mean() for q in range(n_quantiles)]
    return out


def _buckets(values: np.ndarray, n_quantiles: int) -> np.ndarray:
    """Equal-count bucket 0..n-1 of each value by rank (ties by position)."""
    order = np.argsort(values, kind="stable")
    ranks = np.empty(len(values), dtype=int)
    ranks[order] = np.arange(len(values))
    return (ranks * n_quantiles) // len(values)


def _turnover(scores: np.ndarray, n_quantiles: int, min_names: int) -> tuple[float, float]:
    """``(1 - mean rank autocorrelation, mean top-bucket replacement)``
    between consecutive dates with enough scored names."""
    corr: list[float] = []
    replaced: list[float] = []
    prev: np.ndarray | None = None
    for t in range(scores.shape[0]):
        row = scores[t]
        if np.isfinite(row).sum() < max(min_names, n_quantiles):
            continue
        if prev is not None:
            c = _spearman_rows(prev[None, :], row[None, :], min_names)[0]
            if math.isfinite(c):
                corr.append(c)
            replaced.append(1.0 - _overlap(_top(prev, n_quantiles), _top(row, n_quantiles)))
        prev = row
    return (
        1.0 - float(np.mean(corr)) if corr else math.nan,
        float(np.mean(replaced)) if replaced else math.nan,
    )


def _top(row: np.ndarray, n_quantiles: int) -> set[int]:
    idx = np.nonzero(np.isfinite(row))[0]
    buckets = _buckets(row[idx], n_quantiles)
    return set(idx[buckets == n_quantiles - 1].tolist())


def _overlap(before: set[int], after: set[int]) -> float:
    return len(before & after) / len(after) if after else 1.0


# ---- results ----------------------------------------------------------------


@dataclass(frozen=True)
class HorizonIC:
    horizon: int
    #: Sampled dates with an IC (enough names with a score and a return).
    n_dates: int
    mean_ic: float
    ic_std: float
    icir: float
    #: Share of dates with a positive IC.
    hit_rate: float
    se_iid: float
    se_hac: float
    hac_lags: int
    t_stat_hac: float
    #: Mean forward return per score bucket, lowest scores first.
    quantile_means: list[float]
    #: Mean top-minus-bottom bucket return, and its HAC t-stat.
    spread_mean: float
    spread_t_hac: float


@dataclass(frozen=True)
class SignalICResult:
    strategy_id: str
    window: tuple[str, str]
    n_tickers: int
    n_dates: int
    every_bars: int
    n_quantiles: int
    #: ``"ok"`` or ``"n/a"`` (see ``note``).
    status: str
    note: str = ""
    horizons: list[HorizonIC] = field(default_factory=list)
    score_turnover: float = math.nan
    top_quantile_turnover: float = math.nan
    ic_horizon: int | None = None
    ic_estimate: float = math.nan

    def to_dict(self) -> dict[str, Any]:
        """Plain JSON-ready data; NaN becomes ``None``."""
        return _clean(dataclasses.asdict(self))

    def metrics(self) -> dict[str, float]:
        """Flat float metrics (a ``SurvivalReport``'s ``metrics``)."""
        out = {"n_tickers": float(self.n_tickers), "n_dates": float(self.n_dates)}
        if self.status != "ok":
            return out
        out["ic_estimate"] = self.ic_estimate
        out["ic_horizon"] = float(self.ic_horizon or 0)
        out["score_turnover"] = self.score_turnover
        out["top_quantile_turnover"] = self.top_quantile_turnover
        for h in self.horizons:
            out[f"ic_mean_h{h.horizon}"] = h.mean_ic
            out[f"icir_h{h.horizon}"] = h.icir
            out[f"ic_t_hac_h{h.horizon}"] = h.t_stat_hac
            out[f"spread_h{h.horizon}"] = h.spread_mean
        return out


def _clean(value: Any) -> Any:
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_clean(v) for v in value]
    return value


def _ic_horizon(strategy: Any, horizons: Sequence[int]) -> int:
    label = int(getattr(strategy, "label_horizon_bars", 0) or 0)
    if label > 0:
        return min(horizons, key=lambda h: (abs(h - label), -h))
    return DEFAULT_IC_HORIZON if DEFAULT_IC_HORIZON in horizons else max(horizons)


def _check_args(horizons: Sequence[int], every_bars: int, n_quantiles: int) -> tuple[int, ...]:
    hs = tuple(sorted({int(h) for h in horizons}))
    if not hs:
        raise ValueError("horizons must not be empty")
    if hs[0] < 1:
        raise ValueError(f"horizons must be >= 1 bar, got {list(horizons)}")
    if every_bars < 1:
        raise ValueError(f"every_bars must be >= 1, got {every_bars}")
    if n_quantiles < 2:
        raise ValueError(f"n_quantiles must be >= 2, got {n_quantiles}")
    return hs


def signal_ic(
    strategy: Strategy,
    dataset: Any,
    horizons: Sequence[int] = DEFAULT_HORIZONS,
    every_bars: int = 5,
    *,
    window: tuple[date, date] | None = None,
    n_quantiles: int = 5,
    min_names: int = 5,
    max_workers: int | None = None,
) -> SignalICResult:
    """IC analysis of ``strategy.estimate_return`` over ``dataset``'s
    universe and ``window`` (default: the full window). See module doc."""
    hs = _check_args(horizons, every_bars, n_quantiles)
    window = window or dataset.full_window
    universe = list(dataset.universe)
    base = {
        "strategy_id": str(getattr(strategy, "id", type(strategy).__name__)),
        "window": (str(window[0]), str(window[1])),
        "n_tickers": len(universe),
        "every_bars": every_bars,
        "n_quantiles": n_quantiles,
    }
    if len(universe) < MIN_UNIVERSE:
        return SignalICResult(
            **base,
            n_dates=0,
            status="n/a",
            note=f"universe has {len(universe)} tickers; IC analysis needs at least {MIN_UNIVERSE}",
        )
    bars = window_bars(dataset, window)
    timeline = sampled_timeline(bars, every_bars)
    _log.info(
        "signal_ic.start", strategy=base["strategy_id"], tickers=len(universe), dates=len(timeline)
    )
    scores = scores_on(strategy, dataset, bars, timeline, max_workers=max_workers)
    fwd = forward_returns_from_bars(bars, hs, timeline, universe)
    s = scores.to_numpy(float)
    per_h: list[HorizonIC] = []
    for h in hs:
        r = fwd[h].to_numpy(float)
        lags = max(0, math.ceil(h / every_bars) - 1)
        ic = _spearman_rows(s, r, min_names)
        mean, std, se_iid, se_hac = _mean_se(ic, lags)
        valid = ic[np.isfinite(ic)]
        q = _quantile_returns(s, r, n_quantiles, min_names)
        spread = q[:, -1] - q[:, 0]
        spread_mean, _, _, spread_se = _mean_se(spread, lags)
        per_h.append(
            HorizonIC(
                horizon=h,
                n_dates=int(valid.size),
                mean_ic=mean,
                ic_std=std,
                icir=_ratio(mean, std),
                hit_rate=float((valid > 0).mean()) if valid.size else math.nan,
                se_iid=se_iid,
                se_hac=se_hac,
                hac_lags=lags,
                t_stat_hac=_ratio(mean, se_hac) if math.isfinite(mean) else math.nan,
                quantile_means=[
                    float(np.nanmean(q[:, k])) if np.isfinite(q[:, k]).any() else math.nan
                    for k in range(n_quantiles)
                ],
                spread_mean=spread_mean,
                spread_t_hac=_ratio(spread_mean, spread_se)
                if math.isfinite(spread_mean)
                else math.nan,
            )
        )
    turnover, top_turnover = _turnover(s, n_quantiles, min_names)
    ic_h = _ic_horizon(strategy, hs)
    return SignalICResult(
        **base,
        n_dates=len(timeline),
        status="ok",
        horizons=per_h,
        score_turnover=turnover,
        top_quantile_turnover=top_turnover,
        ic_horizon=ic_h,
        ic_estimate=next(x.mean_ic for x in per_h if x.horizon == ic_h),
    )


# ---- command line -----------------------------------------------------------


def _parse_ints(raw: str) -> tuple[int, ...]:
    return tuple(int(x) for x in raw.split(",") if x.strip())


def main(argv: Sequence[str] | None = None, *, prog: str | None = None) -> int:
    """``prog`` names the command in usage messages (``stonks lab ic``)."""
    parser = argparse.ArgumentParser(
        prog=prog or "python -m stonks.lab.signal_eval",
        description="Signal IC analysis (and optionally an event study) of a strategy.",
    )
    parser.add_argument("--strategy", required=True, help="catalog id, class name or module:Class")
    parser.add_argument("--params", default="{}", help="strategy params as JSON")
    parser.add_argument("--tickers", required=True, help="comma-separated universe")
    parser.add_argument("--start", type=date.fromisoformat, default=None)
    parser.add_argument("--end", type=date.fromisoformat, default=None)
    parser.add_argument("--interval", default="1d")
    parser.add_argument("--horizons", type=_parse_ints, default=DEFAULT_HORIZONS)
    parser.add_argument("--every-bars", type=int, default=5)
    parser.add_argument("--events", action="store_true", help="also run the event study")
    parser.add_argument("--workers", type=int, default=None, help="worker processes")
    parser.add_argument("--config", type=Path, default=None, help="settings TOML file")
    parser.add_argument("--lake", type=Path, default=None, help="lake file (overrides config)")
    parser.add_argument("--json", type=Path, default=None, help="write the results as JSON")
    parser.add_argument("--html", type=Path, default=None, help="write an HTML page")
    args = parser.parse_args(argv)

    from stonks.config import load_settings
    from stonks.lab.catalog import resolve_strategy
    from stonks.lab.dataset import LabDataset
    from stonks.lab.survival.event_study import event_study
    from stonks.reporting.signals import render_signal_page
    from stonks.store.lake import DuckDBLake

    tickers = [t.strip() for t in args.tickers.split(",") if t.strip()]
    strategy = resolve_strategy(args.strategy)(json.loads(args.params))
    lake_path = (
        args.lake or (load_settings(args.config) if args.config else load_settings()).lake.path
    )
    with DuckDBLake(lake_path, read_only=True) as lake:
        start, end = args.start, args.end
        if start is None or end is None:
            row = lake.sql(
                "SELECT MIN(timestamp) AS lo, MAX(timestamp) AS hi FROM bars"
                " WHERE ticker = ANY(?) AND interval = ?",
                [tickers, args.interval],
            ).iloc[0]
            if pd.isna(row["lo"]):
                parser.error("no bars for those tickers at that interval")
            start = start or pd.Timestamp(row["lo"]).date()
            end = end or pd.Timestamp(row["hi"]).date()
        dataset = LabDataset(
            lake=lake,
            universe=tickers,
            start=start,
            end=end,
            interval=Interval.parse(args.interval),
        )
        ic = signal_ic(strategy, dataset, args.horizons, args.every_bars, max_workers=args.workers)
        events = (
            event_study(strategy, dataset, dataset.full_window, max_workers=args.workers)
            if args.events
            else None
        )
    print(_summary(ic))
    if events is not None:
        print(events.summary())
    if args.json:
        payload = {"ic": ic.to_dict(), "events": events.to_dict() if events else None}
        args.json.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    if args.html:
        args.html.write_text(render_signal_page(ic, events), encoding="utf-8")
    return 0


def _summary(result: SignalICResult) -> str:
    head = f"{result.strategy_id}: {result.n_tickers} tickers, {result.n_dates} dates"
    if result.status != "ok":
        return f"{head}; {result.status}: {result.note}"
    lines = [head, "horizon  mean_ic  icir   t_hac  spread"]
    for h in result.horizons:
        lines.append(
            f"{h.horizon:>7}  {h.mean_ic:+.4f}  {h.icir:+.3f}  {h.t_stat_hac:+.2f}  "
            f"{h.spread_mean:+.4%}"
        )
    lines.append(
        f"turnover {result.score_turnover:.3f}; top-bucket turnover "
        f"{result.top_quantile_turnover:.3f}; ic_estimate {result.ic_estimate:+.4f} "
        f"(h={result.ic_horizon})"
    )
    return "\n".join(lines)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

"""Bar-shuffle permutation utilities and the MonteCarloPermutationTest
(MCPT) survival test.

The permutation preserves per-bar return statistics but destroys
time-ordering, which makes it a proper null for strategies that rely on
temporal structure (momentum, breakout, mean-reversion, …).

Algorithm (in log space):
- relative open ``r_o[i] = log_open[i] - log_close[i-1]`` (gap)
- relative high ``r_h[i] = log_high[i] - log_open[i]``
- relative low  ``r_l[i] = log_low[i]  - log_open[i]``
- relative close ``r_c[i] = log_close[i] - log_open[i]``

A first permutation shuffles the intra-bar ``(r_h, r_l, r_c)`` triple
together (so high/low/close of the same bar stay coupled); a second,
independent permutation shuffles ``r_o`` (gaps). The first
``start_index + 1`` bars are preserved unchanged; the rest are
reconstructed bar-by-bar from the shuffled relatives, exponentiated
back into price space.
"""

from __future__ import annotations

import dataclasses
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from stonks.core.interval import Interval
from stonks.core.protocols import Strategy, SurvivalReport
from stonks.lab.backtesting import run_backtest
from stonks.lab.lake_copy import copy_universe_lake
from stonks.lab.survival.base import TuningSetup
from stonks.lab.tuning.base import fixed_params_of, tune_and_fit
from stonks.logging import get_logger
from stonks.store.lake import DuckDBLake

_log = get_logger("stonks.lab.survival.permutation")

_OHLC_COLS = ("open", "high", "low", "close")


def permute_bars(bars: pd.DataFrame, start_index: int = 0, seed: int | None = None) -> pd.DataFrame:
    """Return a permuted copy of ``bars``.

    ``bars`` is a DataFrame with columns including ``open/high/low/close``.
    Other columns (ticker, timestamp, volume, adj_close) are preserved
    positionally; ``adj_close`` is reset to the permuted ``close``.

    The first ``start_index + 1`` bars are kept as-is; bars after that are
    reconstructed from shuffled log-space relatives.
    """
    if bars.empty or len(bars) <= start_index + 1:
        return bars.copy()

    df = bars.reset_index(drop=True).copy()
    perm_index = start_index + 1
    n = len(df)
    perm_n = n - perm_index

    rng = np.random.default_rng(seed)
    log_open = np.log(df["open"].to_numpy(dtype=float))
    log_high = np.log(df["high"].to_numpy(dtype=float))
    log_low = np.log(df["low"].to_numpy(dtype=float))
    log_close = np.log(df["close"].to_numpy(dtype=float))

    # relatives
    r_o = np.empty(n)
    r_o[1:] = log_open[1:] - log_close[:-1]
    r_o[0] = 0.0  # unused for the preserved bar
    r_h = log_high - log_open
    r_l = log_low - log_open
    r_c = log_close - log_open

    perm1 = rng.permutation(perm_n)  # intra-bar (h, l, c)
    perm2 = rng.permutation(perm_n)  # gap (o)

    shuffled_h = r_h[perm_index:][perm1]
    shuffled_l = r_l[perm_index:][perm1]
    shuffled_c = r_c[perm_index:][perm1]
    shuffled_o = r_o[perm_index:][perm2]

    # Rebuild the path as one running sum: close[i-1] -> +gap -> open[i]
    # -> +body -> close[i]. Interleaving [anchor, o1, c1, o2, c2, ...] and
    # accumulating (np.add.accumulate is strictly left-to-right) performs
    # exactly the same float additions as the bar-by-bar loop, so outputs
    # are bit-identical to it for a given seed.
    steps = np.empty(2 * perm_n + 1)
    steps[0] = log_close[perm_index - 1]
    steps[1::2] = shuffled_o
    steps[2::2] = shuffled_c
    path = np.cumsum(steps)

    new_log_open = log_open.copy()
    new_log_close = log_close.copy()
    new_log_open[perm_index:] = path[1::2]
    new_log_close[perm_index:] = path[2::2]
    new_log_high = log_high.copy()
    new_log_low = log_low.copy()
    new_log_high[perm_index:] = new_log_open[perm_index:] + shuffled_h
    new_log_low[perm_index:] = new_log_open[perm_index:] + shuffled_l

    out = df.copy()
    out["open"] = np.exp(new_log_open)
    out["high"] = np.exp(new_log_high)
    out["low"] = np.exp(new_log_low)
    out["close"] = np.exp(new_log_close)
    if "adj_close" in out.columns:
        out["adj_close"] = out["close"]
    # volume and any other columns: untouched positionally
    return out


# ---- MCPT survival test ----------------------------------------------------

_METRICS = ("profit_factor", "sharpe", "final_return", "cagr")


class MonteCarloPermutationTest:
    """Monte-Carlo permutation test: is the strategy's real score reliably
    better than its scores on time-shuffled bars?

    Two modes, both scoring only data the strategy's parameters were not
    chosen on unless re-tuning is part of the null:

    - **Out-of-sample (default, ``retune=False``).** The already-tuned
      strategy is scored on the dataset's *validation* window. Each
      permutation shuffles only the validation-window bars; every bar
      before it stays real so indicator look-backs warm up on true
      history, and bars after the window are dropped. Tuning happened on
      the train window, so its selection bias cannot push the p-value
      toward passing.
    - **In-sample with re-tuning (``retune=True``, Masters).** Each
      permutation shuffles only the *train*-window bars (earlier history
      real, later bars dropped) and re-runs the full tune → fit process on
      it; the score is the tuner's best objective value. The real score
      comes from the same process on unshuffled bars. This asks whether
      the whole tune-then-trade process beats noise. It costs
      ``(n_permutations + 1) * budget`` backtests, so it is off by
      default. The tuner / objective / budget come from ``tuning`` or, if
      that is unset, from the ``LabRunner`` via ``bind_tuning``.

    Every score — real and permuted — is computed on an in-memory lake
    built the same way (see ``lab.lake_copy``): the universe's non-bar
    tables copied as-is, the dataset-interval bars (real or permuted) up
    to the window end, and any coarser interval present in the source
    re-derived from those bars so no real coarse prices leak into a
    permuted run. Finer intervals are not carried over.

    The p-value is the +1-smoothed fraction of permuted scores at least as
    good as the real one; the test passes when ``p_value <= max_p_value``.

    In the default mode the same strategy instance is scored on every
    lake; strategies that cache lake reads must key the cache by lake
    (as ``Momentum`` does).
    """

    id = "mcpt"

    def __init__(
        self,
        n_permutations: int = 50,
        max_p_value: float = 0.05,
        metric: str = "profit_factor",
        retune: bool = False,
        seed: int | None = 17,
        tuning: TuningSetup | None = None,
    ) -> None:
        if n_permutations < 1:
            raise ValueError("n_permutations must be >= 1")
        if not 0.0 < max_p_value <= 1.0:
            raise ValueError("max_p_value must be in (0, 1]")
        if metric not in _METRICS:
            raise ValueError(f"unsupported metric {metric!r}")
        self._n = n_permutations
        self._max_p = max_p_value
        self._metric = metric
        self._retune = retune
        self._seed = seed
        self._tuning = tuning
        self._bound: TuningSetup | None = None

    def bind_tuning(self, setup: TuningSetup) -> None:
        """Receive the runner's tuning setup (used when ``tuning`` is unset)."""
        self._bound = setup

    def run(self, strategy: Strategy, context: Any) -> SurvivalReport:
        setup = self._tuning or self._bound
        if self._retune and setup is None:
            raise ValueError(
                "MCPT retune=True needs a tuning setup: pass tuning=... or run it under LabRunner"
            )
        window = context.train_window if self._retune else context.val_window
        interval = getattr(context, "interval", Interval.DAY_1)
        mode = "retune" if self._retune else "oos"
        _log.info(
            "mcpt.start", mode=mode, seed=self._seed, n=self._n, window=[str(w) for w in window]
        )

        history = _history_bars(context, interval, window)
        if not any(len(bars) > n_before for bars, n_before in history.values()):
            return SurvivalReport(
                test_id=self.id,
                passed=False,
                metrics={"p_value": 1.0, "real_score": 0.0, "n_permutations": float(self._n)},
                notes=f"mode={mode}; no bars in the scored window for the universe; test skipped",
            )
        coarser = _coarser_intervals(context, interval)
        fixed = fixed_params_of(strategy)  # re-tunes keep e.g. a wrapper's inner

        def score(bars_by_ticker: dict[str, pd.DataFrame]) -> float:
            with _modified_lake(context, bars_by_ticker, interval, coarser) as lake:
                dataset = dataclasses.replace(context, lake=lake)
                if self._retune:
                    _, tuned = tune_and_fit(type(strategy), dataset, setup, fixed)
                    return float(tuned.best_score)
                report = run_backtest(strategy, dataset, window)
                return float(getattr(report, self._metric))

        real_score = score({t: bars for t, (bars, _) in history.items()})
        minimize = self._retune and setup.objective.direction == "minimize"

        master_rng = np.random.default_rng(self._seed)
        perm_scores: list[float] = []
        at_least_as_good = 0
        for _ in range(self._n):
            # permute_bars keeps its first ``start_index + 1`` rows: exactly
            # the ``n_before`` pre-window bars (the first window bar when
            # there is no earlier history, as the path needs an anchor).
            permuted = {
                ticker: permute_bars(
                    bars,
                    start_index=max(n_before - 1, 0),
                    seed=int(master_rng.integers(0, 2**31 - 1)),
                )
                for ticker, (bars, n_before) in history.items()
            }
            perm_score = score(permuted)
            perm_scores.append(perm_score)
            if (perm_score <= real_score) if minimize else (perm_score >= real_score):
                at_least_as_good += 1

        # +1 numerator/denominator — conservative p-value estimator.
        p_value = (at_least_as_good + 1) / (self._n + 1)
        passed = p_value <= self._max_p

        arr = np.asarray(perm_scores, dtype=float)
        metric = setup.objective.name if self._retune else self._metric
        return SurvivalReport(
            test_id=self.id,
            passed=passed,
            metrics={
                "p_value": float(p_value),
                "real_score": float(real_score),
                "perm_score_mean": float(arr.mean()) if arr.size else 0.0,
                "perm_score_max": float(arr.max()) if arr.size else 0.0,
                "n_permutations": float(self._n),
            },
            notes=f"mode={mode}; metric={metric}; seed={self._seed}",
        )


def _history_bars(
    context: Any, interval: Interval, window: tuple[date, date]
) -> dict[str, tuple[pd.DataFrame, int]]:
    """Per ticker: every dataset-interval bar up to the window end (all
    earlier history included, for look-backs) and how many of them fall
    before the window start."""
    start, end = window
    frame = context.lake.sql(
        """
        SELECT ticker, timestamp, open, high, low, close, adj_close, volume
          FROM bars
         WHERE ticker = ANY(?) AND interval = ? AND CAST(timestamp AS DATE) <= ?
         ORDER BY ticker, timestamp
        """,
        [list(context.universe), interval.code, end],
    )
    out: dict[str, tuple[pd.DataFrame, int]] = {}
    for ticker, bars in frame.groupby("ticker", sort=False):
        bars = bars.reset_index(drop=True)
        n_before = int((pd.to_datetime(bars["timestamp"]).dt.date < start).sum())
        out[str(ticker)] = (bars, n_before)
    return out


def _coarser_intervals(context: Any, interval: Interval) -> list[Interval]:
    codes = context.lake.sql(
        "SELECT DISTINCT interval FROM bars WHERE ticker = ANY(?)", [list(context.universe)]
    )["interval"]
    found = [Interval.parse(c) for c in codes]
    return sorted((i for i in found if i.seconds > interval.seconds), key=lambda i: i.seconds)


def _modified_lake(
    context: Any,
    bars_by_ticker: dict[str, pd.DataFrame],
    interval: Interval,
    coarser: list[Interval],
) -> DuckDBLake:
    lake = copy_universe_lake(context.lake, context.universe)
    try:
        for ticker, bars in bars_by_ticker.items():
            if bars.empty:
                continue
            lake.upsert_bars(bars, interval=interval)
            for target in coarser:
                lake.aggregate_bars(ticker, source=interval, target=target)
    except Exception:
        lake.close()
        raise
    return lake

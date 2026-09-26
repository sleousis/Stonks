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

import copy
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from stonks.core.interval import Interval
from stonks.core.protocols import Strategy, SurvivalReport
from stonks.lab.backtesting import run_backtest
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


@dataclass(frozen=True)
class _McptScoring:
    metric: str  # "profit_factor" | "sharpe" | "final_return" | "cagr"
    direction: str = "maximize"


class MonteCarloPermutationTest:
    """Monte-Carlo permutation test: is the strategy's real score reliably
    better than scores on time-shuffled bars?

    For each of ``n_permutations`` runs we build an in-memory DuckDBLake,
    copy the universe's bars into it under a fresh bar-shuffle permutation,
    and score the strategy against the permuted lake. The p-value is the
    fraction of permuted scores greater-or-equal to the real score
    (conservatively +1-smoothed). The test passes when ``p_value`` falls
    below ``max_p_value``.

    Known limitation: this is an in-sample test without re-tuning. The
    strategy keeps the params (and fitted state) chosen on the real bars
    and is only re-scored on each permutation; it is not re-tuned or
    re-fitted per permutation. It therefore asks "does this fixed
    configuration beat noise?", not "does the whole tune-then-trade
    process beat noise?", and understates the selection bias of tuning.
    """

    id = "mcpt"

    def __init__(
        self,
        n_permutations: int = 50,
        max_p_value: float = 0.05,
        metric: str = "profit_factor",
        start_index_ratio: float = 0.0,
        seed: int | None = 17,
    ) -> None:
        if n_permutations < 1:
            raise ValueError("n_permutations must be >= 1")
        if not 0.0 < max_p_value <= 1.0:
            raise ValueError("max_p_value must be in (0, 1]")
        if metric not in ("profit_factor", "sharpe", "final_return", "cagr"):
            raise ValueError(f"unsupported metric {metric!r}")
        if not 0.0 <= start_index_ratio < 1.0:
            raise ValueError("start_index_ratio must be in [0, 1)")
        self._n = n_permutations
        self._max_p = max_p_value
        self._metric = metric
        self._start_ratio = start_index_ratio
        self._seed = seed

    def run(self, strategy: Strategy, context) -> SurvivalReport:
        interval = getattr(context, "interval", Interval.DAY_1)
        universe = list(context.universe)
        start, end = context.full_window

        real_bars_by_ticker: dict[str, pd.DataFrame] = {}
        for ticker in universe:
            bars = context.lake.get_bars(ticker, interval, start=start, end=end)
            if not bars.empty:
                real_bars_by_ticker[ticker] = bars

        if not real_bars_by_ticker:
            return SurvivalReport(
                test_id=self.id,
                passed=False,
                metrics={"p_value": 1.0, "real_score": 0.0, "n_permutations": float(self._n)},
                notes="no bars available for the universe; test skipped",
            )

        real_score = _score(strategy, context, context.lake, self._metric)

        master_rng = np.random.default_rng(self._seed)
        perm_scores: list[float] = []
        worse_or_equal = 0
        for _ in range(self._n):
            permuted = {
                ticker: permute_bars(
                    bars,
                    start_index=int(len(bars) * self._start_ratio),
                    seed=int(master_rng.integers(0, 2**31 - 1)),
                )
                for ticker, bars in real_bars_by_ticker.items()
            }
            with _build_permuted_lake(permuted, interval) as perm_lake:
                perm_score = _score(strategy, context, perm_lake, self._metric)
            perm_scores.append(perm_score)
            if perm_score >= real_score:
                worse_or_equal += 1

        # +1 numerator/denominator — conservative p-value estimator.
        p_value = (worse_or_equal + 1) / (self._n + 1)
        passed = p_value <= self._max_p

        arr = np.asarray(perm_scores, dtype=float)
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
            notes=f"metric={self._metric}",
        )


def _score(strategy: Strategy, context, lake: DuckDBLake, metric: str) -> float:
    report = run_backtest(strategy, context, context.full_window, lake=lake)
    return float(getattr(report, metric))


class _InMemLakeCtx:
    """tiny context manager wrapper around a DuckDBLake for the `with` in run()."""

    def __init__(self, lake: DuckDBLake) -> None:
        self._lake = lake

    def __enter__(self) -> DuckDBLake:
        return self._lake

    def __exit__(self, *exc) -> None:
        self._lake.close()


def _build_permuted_lake(
    permuted_bars_by_ticker: dict[str, pd.DataFrame],
    interval: Interval,
) -> _InMemLakeCtx:
    lake = DuckDBLake(Path(":memory:"))
    lake.migrate()
    for bars in permuted_bars_by_ticker.values():
        if bars.empty:
            continue
        lake.upsert_bars(bars, interval=interval)
    return _InMemLakeCtx(lake)


# silence unused-import warnings in tests that reach into this module
_ = copy  # noqa: F841

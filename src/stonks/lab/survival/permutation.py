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

For a multi-ticker universe the MCPT uses :func:`permute_bars_together`:
both permutations are drawn once over the union timeline and applied to
every ticker (Masters' multi-market MCPT), so the null keeps cross-asset
correlation and only destroys time structure.
"""

from __future__ import annotations

import dataclasses
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, ClassVar, Literal, Protocol

import numpy as np
import pandas as pd

from stonks.core.interval import Interval
from stonks.core.protocols import Strategy, SurvivalReport
from stonks.features.price_adjustment import SeriesAdjustment
from stonks.lab.backtesting import run_backtest
from stonks.lab.dataset import data_tickers, scoring_window
from stonks.lab.lake_copy import CORPORATE_ACTION_TABLES, copy_universe_lake
from stonks.lab.parallel import PortableLake, PortableStrategy, run_tasks
from stonks.lab.survival.base import TuningSetup
from stonks.lab.tuning.base import tune_and_fit
from stonks.logging import get_logger
from stonks.store.corporate_actions import LakeCorporateActions
from stonks.store.lake import DuckDBLake

_log = get_logger("stonks.lab.survival.permutation")

_OHLC_COLS = ("open", "high", "low", "close")

#: Default MCPT permutation count (BL-21): the smallest p-value it can
#: reach is 1 / 201.
DEFAULT_N_PERMUTATIONS = 200


def has_nontrivial_fit(strategy: Any) -> bool:
    """True when ``strategy``'s class overrides ``BaseStrategy.fit`` (it
    learns state from the train window, as ML strategies do). Wrappers
    that delegate ``fit`` count as fitted."""
    from stonks.strategies.base import BaseStrategy

    fit = getattr(type(strategy), "fit", None)
    return callable(fit) and fit is not BaseStrategy.fit


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
    rng = np.random.default_rng(seed)
    perm_n = len(bars) - (start_index + 1)
    perm1 = rng.permutation(perm_n)  # intra-bar (h, l, c)
    perm2 = rng.permutation(perm_n)  # gap (o)
    return _apply_permutation(bars, start_index, perm1, perm2)


def permute_bars_together(
    history: Mapping[str, tuple[pd.DataFrame, int]], seed: int | None = None
) -> dict[str, pd.DataFrame]:
    """Permute several tickers' bars with **one shared ordering** (Masters'
    multi-market permutation), so cross-asset correlation survives: on a
    common timeline every ticker's bar at a given slot draws its relatives
    from the same source timestamp.

    ``history`` maps ticker -> ``(bars, start_index)`` with the same
    ``start_index`` meaning as :func:`permute_bars`. The permutation is
    drawn over the sorted union of every ticker's permutable timestamps;
    each ticker keeps the union order restricted to its own bars. With a
    single ticker (or identical timelines) this is exactly
    :func:`permute_bars` for the same ``seed``; a ticker missing bars on
    some timestamps keeps the shared order of the ones it has.
    """
    permutable: dict[str, np.ndarray] = {}
    for ticker, (bars, start_index) in history.items():
        if len(bars) > start_index + 1:
            ts = pd.to_datetime(bars["timestamp"]).to_numpy()[start_index + 1 :]
            permutable[ticker] = ts
    out = {t: bars.copy() for t, (bars, _) in history.items()}
    if not permutable:
        return out
    union = np.unique(np.concatenate(list(permutable.values())))
    rng = np.random.default_rng(seed)
    shared1 = rng.permutation(len(union))  # intra-bar (h, l, c)
    shared2 = rng.permutation(len(union))  # gap (o)
    for ticker, ts in permutable.items():
        bars, start_index = history[ticker]
        # local index of each union slot this ticker has, -1 elsewhere
        local = np.full(len(union), -1)
        local[np.searchsorted(union, ts)] = np.arange(len(ts))
        perm1 = local[shared1]
        perm2 = local[shared2]
        out[ticker] = _apply_permutation(bars, start_index, perm1[perm1 >= 0], perm2[perm2 >= 0])
    return out


def _apply_permutation(
    bars: pd.DataFrame, start_index: int, perm1: np.ndarray, perm2: np.ndarray
) -> pd.DataFrame:
    """Rebuild ``bars`` after ``start_index`` from its log relatives,
    reordered by ``perm1`` (intra-bar high/low/close) and ``perm2`` (gaps)."""
    df = bars.reset_index(drop=True).copy()
    perm_index = start_index + 1
    n = len(df)
    perm_n = n - perm_index

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

_METRICS = ("profit_factor", "bar_profit_factor", "sharpe", "final_return", "cagr")
#: Option names that read a differently named report field (RS-35: the
#: ``profit_factor`` option reads ``bar_profit_factor``, not the
#: deprecated report alias).
_REPORT_FIELD = {"profit_factor": "bar_profit_factor"}


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

    Corporate actions: the bars scored (real and permuted) are
    back-adjusted as of the window end (splits and dividends folded into
    the prices, see ``features.price_adjustment``) and the lakes carry no
    ``stock_splits`` / ``dividends`` rows. Shuffling raw bars would move a
    split's price drop to a random bar while the engine still multiplied
    the position on the real ex-date, so equity would jump by the ratio.

    ``retune="auto"`` picks the mode per strategy: re-tuning for a
    strategy with a non-trivial ``fit`` (an ML strategy, see
    :func:`has_nontrivial_fit`), out-of-sample otherwise. The promotion
    preset uses it.

    The OOS window is the dataset's validation window embargoed for the
    strategy (``lab.dataset.scoring_window``).

    The p-value is the +1-smoothed fraction of permuted scores at least as
    good as the real one; the test passes when ``p_value <= max_p_value``.
    ``n_permutations`` defaults to 200 (BL-21; it was 50), so the smallest
    reachable p-value is 1/201 (about 0.005), well under the 0.05 gate.

    Permutations run on a process pool of ``max_workers`` (default
    ``lab.parallel.default_max_workers()``; 1 runs them in-process). Every
    permutation's seed is drawn from ``seed`` up front, so the report is
    bit-identical for any worker count, provided the strategy's
    ``save``/``load`` round-trip is exact (workers get a reloaded copy).

    In the default mode the same strategy instance is scored on every
    lake; strategies that cache lake reads must key the cache by lake
    (as ``Momentum`` does).
    """

    id = "mcpt"

    #: Plain words for each option, shown by the console's options editor.
    option_help: ClassVar[dict[str, str]] = {
        "n_permutations": "How many shuffled price histories to test against.",
        "max_p_value": "Highest share of shuffled histories that may do as well as the real one.",
        "metric": "Which result to compare between real and shuffled histories.",
        "retune": "Tune again on each shuffled history. auto does it when the strategy has settings to tune.",
    }

    def __init__(
        self,
        n_permutations: int = DEFAULT_N_PERMUTATIONS,
        max_p_value: float = 0.05,
        metric: str = "profit_factor",
        retune: bool | Literal["auto"] = False,
        seed: int | None = 17,
        tuning: TuningSetup | None = None,
        max_workers: int | None = None,
    ) -> None:
        if n_permutations < 1:
            raise ValueError("n_permutations must be >= 1")
        if not 0.0 < max_p_value <= 1.0:
            raise ValueError("max_p_value must be in (0, 1]")
        if metric not in _METRICS:
            raise ValueError(f"unsupported metric {metric!r}")
        if max_workers is not None and max_workers < 1:
            raise ValueError("max_workers must be >= 1")
        if retune not in (True, False, "auto"):
            raise ValueError(f"retune must be True, False or 'auto', got {retune!r}")
        self._n = n_permutations
        self._max_p = max_p_value
        self._metric = metric
        self._retune = retune
        self._seed = seed
        self._tuning = tuning
        self._bound: TuningSetup | None = None
        self._max_workers = max_workers

    def bind_tuning(self, setup: TuningSetup) -> None:
        """Receive the runner's tuning setup (used when ``tuning`` is unset)."""
        self._bound = setup

    def run(self, strategy: Strategy, context: Any) -> SurvivalReport:
        setup = self._tuning or self._bound
        retune = has_nontrivial_fit(strategy) if self._retune == "auto" else bool(self._retune)
        if retune and setup is None:
            raise ValueError(
                "MCPT re-tuning needs a tuning setup: pass tuning=... or run it under LabRunner"
            )
        window = context.train_window if retune else scoring_window(context, strategy)
        mode = "retune" if retune else "oos"
        _log.info(
            "mcpt.start", mode=mode, seed=self._seed, n=self._n, window=[str(w) for w in window]
        )
        if retune:
            # re-tunes keep e.g. a wrapper's inner strategy and any pinned params
            evaluate: Evaluator = RetuneScore(setup, setup.retune_fixed_params(strategy))
        else:
            evaluate = BacktestScore(self._metric, window)
        scorer = PermutationScorer.build(strategy, context, window, evaluate)
        if scorer is None:
            return SurvivalReport(
                test_id=self.id,
                passed=False,
                metrics={"p_value": 1.0, "real_score": 0.0, "n_permutations": float(self._n)},
                notes=f"mode={mode}; no bars in the scored window for the universe; test skipped",
            )

        real_score = scorer.score_real()
        if math.isnan(real_score):
            # a NaN real score beats nothing; without this guard every
            # comparison is False and p would be 1/(n+1), a pass
            return SurvivalReport(
                test_id=self.id,
                passed=False,
                metrics={
                    "p_value": 1.0,
                    "real_score": real_score,
                    "n_permutations": float(self._n),
                },
                notes=f"mode={mode}; insufficient data: the real run has no score (NaN)",
            )
        perm_scores = permuted_scores(scorer, self._n, self._seed, self._max_workers)
        minimize = retune and setup.objective.direction == "minimize"
        p_value = permutation_p_value(real_score, perm_scores, minimize=minimize)
        arr = np.asarray(perm_scores, dtype=float)
        metric = setup.objective.name if retune else self._metric
        return SurvivalReport(
            test_id=self.id,
            passed=p_value <= self._max_p,
            metrics={
                "p_value": float(p_value),
                "real_score": float(real_score),
                "perm_score_mean": float(arr.mean()) if arr.size else 0.0,
                "perm_score_max": float(arr.max()) if arr.size else 0.0,
                "n_permutations": float(self._n),
            },
            notes=f"mode={mode}; metric={metric}; seed={self._seed}",
        )


# ---- shared permutation machinery ------------------------------------------
#
# Used by the MCPT above and the walk-forward permutation test: an
# ``Evaluator`` turns (strategy, dataset on a modified lake) into a score; a
# ``PermutationScorer`` builds that lake from real or permuted bars; and
# ``permuted_scores`` fans the permutations out over ``lab.parallel``.


class Evaluator(Protocol):
    """Scores a strategy on a dataset whose lake holds real or permuted
    bars. Must be picklable (it is shipped to worker processes)."""

    def __call__(self, strategy: Strategy, dataset: Any) -> float: ...


@dataclass(frozen=True)
class BacktestScore:
    """One backtest of the given (already fitted) strategy over ``window``."""

    metric: str
    window: tuple[date, date]

    def __call__(self, strategy: Strategy, dataset: Any) -> float:
        report = run_backtest(strategy, dataset, self.window)
        return float(getattr(report, _REPORT_FIELD.get(self.metric, self.metric)))


@dataclass(frozen=True)
class RetuneScore:
    """The tuner's best score after re-running tune → fit on the dataset."""

    setup: TuningSetup
    fixed: Mapping[str, Any]

    def __call__(self, strategy: Strategy, dataset: Any) -> float:
        _, tuned = tune_and_fit(type(strategy), dataset, self.setup, dict(self.fixed))
        return float(tuned.best_score)


@dataclass
class PermutationScorer:
    """Everything needed to score one set of bars; the per-worker state of
    a permutation run. ``history`` maps ticker -> (bars up to the window
    end, number of those bars before the window start): the permutable
    bars are exactly the ones from the window start on."""

    context: Any  # the dataset, lake detached (``source`` carries it)
    source: PortableLake
    strategy: PortableStrategy
    evaluate: Evaluator
    interval: Interval
    coarser: list[Interval]
    history: dict[str, tuple[pd.DataFrame, int]]

    @classmethod
    def build(
        cls, strategy: Strategy, context: Any, window: tuple[date, date], evaluate: Evaluator
    ) -> PermutationScorer | None:
        """``None`` when the universe has no bars in ``window``."""
        interval = getattr(context, "interval", Interval.DAY_1)
        history = _history_bars(context, interval, window)
        if not any(len(bars) > n_before for bars, n_before in history.values()):
            return None
        return cls(
            context=dataclasses.replace(context, lake=None),
            source=PortableLake(context.lake, data_tickers(context)),
            strategy=PortableStrategy(strategy),
            evaluate=evaluate,
            interval=interval,
            coarser=_coarser_intervals(context, interval),
            history=history,
        )

    def score_real(self) -> float:
        return self.score({t: bars for t, (bars, _) in self.history.items()})

    def score_permutation(self, seed: int) -> float:
        # The permutation keeps each ticker's first ``start_index + 1`` rows:
        # exactly its ``n_before`` pre-window bars (the first window bar when
        # there is no earlier history, as the path needs an anchor). One
        # shared ordering for the whole universe keeps cross-asset
        # correlation intact.
        permuted = permute_bars_together(
            {t: (bars, max(n_before - 1, 0)) for t, (bars, n_before) in self.history.items()},
            seed=seed,
        )
        return self.score(permuted)

    def score(self, bars_by_ticker: dict[str, pd.DataFrame]) -> float:
        with _modified_lake(
            self.source.lake,
            data_tickers(self.context),
            bars_by_ticker,
            self.interval,
            self.coarser,
        ) as lake:
            dataset = dataclasses.replace(self.context, lake=lake)
            return self.evaluate(self.strategy.strategy, dataset)


def permutation_seeds(n: int, seed: int | None) -> list[int]:
    """The per-permutation seeds, drawn up front so results never depend
    on how permutations are spread over workers."""
    master_rng = np.random.default_rng(seed)
    return [int(master_rng.integers(0, 2**31 - 1)) for _ in range(n)]


def permuted_scores(
    scorer: PermutationScorer, n: int, seed: int | None, max_workers: int | None
) -> list[float]:
    """Scores of ``n`` permutations, in seed order, on ``max_workers``
    processes (see ``lab.parallel``)."""
    return run_tasks(
        _score_permutation,
        permutation_seeds(n, seed),
        payload=scorer,
        max_workers=max_workers,
    )


def _score_permutation(scorer: PermutationScorer, seed: int) -> float:
    return scorer.score_permutation(seed)


def permutation_p_value(real: float, permuted: list[float], *, minimize: bool = False) -> float:
    """+1-smoothed share of permuted scores at least as good as ``real``:
    ``(count + 1) / (n + 1)`` — a conservative estimator. A NaN permuted
    score counts as at least as good: it is no evidence against the null."""
    at_least_as_good = sum(
        1 for s in permuted if math.isnan(s) or (s <= real if minimize else s >= real)
    )
    return (at_least_as_good + 1) / (len(permuted) + 1)


def _history_bars(
    context: Any, interval: Interval, window: tuple[date, date]
) -> dict[str, tuple[pd.DataFrame, int]]:
    """Per ticker of ``data_tickers(context)`` (the universe plus the
    tickers the strategy reads, so references are permuted together with
    the universe, RS-01): every dataset-interval bar up to the window end
    (all earlier history included, for look-backs), back-adjusted as of
    its last bar, and how many of them fall before the window start."""
    start, end = window
    tickers = data_tickers(context)
    frame = context.lake.sql(
        """
        SELECT ticker, timestamp, open, high, low, close, adj_close, volume
          FROM bars
         WHERE ticker = ANY(?) AND interval = ? AND CAST(timestamp AS DATE) <= ?
         ORDER BY ticker, timestamp
        """,
        [tickers, interval.code, end],
    )
    actions = LakeCorporateActions(context.lake).load(tickers)
    out: dict[str, tuple[pd.DataFrame, int]] = {}
    for ticker, bars in frame.groupby("ticker", sort=False):
        bars = _adjusted(bars.reset_index(drop=True), actions.for_ticker(str(ticker)))
        n_before = int((pd.to_datetime(bars["timestamp"]).dt.date < start).sum())
        out[str(ticker)] = (bars, n_before)
    return out


def _adjusted(bars: pd.DataFrame, events: Sequence[Any]) -> pd.DataFrame:
    """``bars`` back-adjusted as of the last bar, with ``adj_close`` equal
    to the adjusted close (nothing left to adjust). Identity for a ticker
    with no events and a constant ``adj_close / close``."""
    adjustment = SeriesAdjustment.build(bars, events)
    out = adjustment.apply(bars, 0, len(bars))
    if not adjustment.is_identity and "adj_close" in out.columns:
        out["adj_close"] = out["close"]
    return out


def _coarser_intervals(context: Any, interval: Interval) -> list[Interval]:
    codes = context.lake.sql(
        "SELECT DISTINCT interval FROM bars WHERE ticker = ANY(?)", [data_tickers(context)]
    )["interval"]
    found = [Interval.parse(c) for c in codes]
    return sorted((i for i in found if i.seconds > interval.seconds), key=lambda i: i.seconds)


def _modified_lake(
    source: DuckDBLake,
    universe: list[str],
    bars_by_ticker: dict[str, pd.DataFrame],
    interval: Interval,
    coarser: list[Interval],
) -> DuckDBLake:
    # bars are adjusted: carrying the events would apply them twice
    lake = copy_universe_lake(source, universe, skip_tables=("bars", *CORPORATE_ACTION_TABLES))
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

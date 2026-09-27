"""Stress survival test: alternative validation windows (BL-48; Ruppert and
Matteson, Danielsson, Tsay).

One validation window is one draw from history. This test replays the
strategy on ``n_paths`` (200) simulated alternatives of it and looks at
the bad tail:

- ``block_bootstrap`` (default): a stationary block bootstrap (Politis and
  Romano) of whole bars. Each simulated day copies the gap, body and wicks
  of a historical day, and runs of consecutive days (mean length
  ``block``, 20) are copied together, so volatility clusters survive.
- ``garch_fhs``: filtered historical simulation. Each ticker gets a
  GARCH(1,1)-t fit (``features.vol_forecast.GarchVol``) on its source
  returns, and the close-to-close return of each simulated day is the
  fitted recursion driven by a bootstrapped standardised residual, so a
  calm draw can meet a volatile state. The gap and wicks come from the
  same historical day.

Days are drawn once per path on the union timeline of every ticker the
strategy reads (the universe, references and the benchmark), so every
ticker draws the same historical day and cross-asset correlation is kept.
``source="full"`` draws from every day up to the window's end,
``source="val"`` from the window itself. Bars before the window are
never changed, so look-backs read real history. Prices are back-adjusted
(corporate actions folded in) like the MCPT's.

Passing requires the 5th-percentile path Sharpe above ``min_p5_sharpe``
(-0.5) and the 95th-percentile maximum drawdown (the drawdown only 5% of
paths beat) no deeper than ``max_drawdown_limit`` (-30%).

Paths run as ``lab.parallel.run_tasks`` tasks. Every draw is made up front
from ``seed``, so the report is the same for any worker count.
"""

from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

from stonks.core.interval import Interval
from stonks.core.protocols import Strategy, SurvivalReport
from stonks.features.vol_forecast import GarchVol
from stonks.lab.backtesting import run_backtest
from stonks.lab.dataset import data_tickers, scoring_window
from stonks.lab.parallel import PortableLake, PortableStrategy, run_tasks
from stonks.lab.survival.permutation import _coarser_intervals, _history_bars, _modified_lake
from stonks.logging import get_logger

_log = get_logger("stonks.lab.survival.stress")


def stationary_bootstrap(
    n_source: int, length: int, block: float, rng: np.random.Generator
) -> np.ndarray:
    """``length`` indices into ``range(n_source)``: runs of consecutive
    indices (wrapping at the end) whose lengths are geometric with mean
    ``block``."""
    if n_source < 1 or length < 0:
        raise ValueError("need at least one source row and a non-negative length")
    if block < 1:
        raise ValueError(f"block must be >= 1, got {block}")
    out = np.empty(length, dtype=int)
    restart = rng.random(length) < 1.0 / block
    starts = rng.integers(0, n_source, length)
    current = int(starts[0]) if length else 0
    for t in range(length):
        if t > 0:
            current = int(starts[t]) if restart[t] else (current + 1) % n_source
        out[t] = current
    return out


def _relatives(bars: pd.DataFrame) -> dict[str, np.ndarray]:
    """Per-row log gap, body and wicks (row 0 has no gap: 0)."""
    o = np.log(bars["open"].to_numpy(dtype=float))
    h = np.log(bars["high"].to_numpy(dtype=float))
    lo = np.log(bars["low"].to_numpy(dtype=float))
    c = np.log(bars["close"].to_numpy(dtype=float))
    gap = np.concatenate([[0.0], o[1:] - c[:-1]])
    return {
        "gap": gap,
        "body": c - o,
        "upper": np.maximum(h - np.maximum(o, c), 0.0),
        "lower": np.maximum(np.minimum(o, c) - lo, 0.0),
        "ret": gap + (c - o),
    }


def resample_window(
    bars: pd.DataFrame,
    n_before: int,
    source_rows: np.ndarray,
    returns: np.ndarray | None = None,
) -> pd.DataFrame:
    """``bars`` with every row from ``n_before`` on rebuilt from the rows
    ``source_rows`` (one per rebuilt row): the gap, body and wicks of the
    source row, or, with ``returns``, the given close-to-close log return
    with the source row's gap and wicks. Earlier rows are unchanged."""
    out = bars.reset_index(drop=True).copy()
    n = len(out)
    if n_before >= n:
        return out
    if len(source_rows) != n - n_before:
        raise ValueError("need one source row per rebuilt row")
    rel = _relatives(out)
    src = np.asarray(source_rows, dtype=int)
    gap = rel["gap"][src]
    body = rel["body"][src] if returns is None else np.asarray(returns, dtype=float) - gap
    first = max(n_before, 1)
    prev_close = float(np.log(out["close"].iloc[first - 1]))
    opens = np.log(out["open"].to_numpy(dtype=float))
    closes = np.log(out["close"].to_numpy(dtype=float))
    offset = first - n_before  # a window starting at row 0 keeps its first bar
    for k in range(offset, len(src)):
        i = n_before + k
        opens[i] = prev_close + gap[k]
        closes[i] = opens[i] + body[k]
        prev_close = closes[i]
    upper = rel["upper"].copy()
    lower = rel["lower"].copy()
    upper[n_before:] = rel["upper"][src]
    lower[n_before:] = rel["lower"][src]
    rebuilt = slice(first, n)
    o, c = np.exp(opens[rebuilt]), np.exp(closes[rebuilt])
    out.loc[first:, "open"] = o
    out.loc[first:, "close"] = c
    out.loc[first:, "high"] = np.maximum(o, c) * np.exp(upper[rebuilt])
    out.loc[first:, "low"] = np.minimum(o, c) * np.exp(-lower[rebuilt])
    if "adj_close" in out.columns:
        out.loc[first:, "adj_close"] = c
    if "volume" in out.columns:
        out.loc[first:, "volume"] = out["volume"].to_numpy()[src[offset:]]
    return out


@dataclass(frozen=True)
class _PathRun:
    context: Any
    source: PortableLake
    strategy: PortableStrategy
    history: dict[str, tuple[pd.DataFrame, int]]
    interval: Interval
    coarser: list[Interval]
    window: tuple[date, date]


@dataclass(frozen=True)
class _Path:
    #: ``ticker -> source row per window row`` (local row indices).
    rows: dict[str, np.ndarray]
    #: ``ticker -> simulated log returns`` (``garch_fhs``), else empty.
    returns: dict[str, np.ndarray]


def _score_path(run: _PathRun, path: _Path) -> tuple[float, float]:
    bars_by_ticker = {
        ticker: resample_window(bars, n_before, path.rows[ticker], path.returns.get(ticker))
        if ticker in path.rows
        else bars
        for ticker, (bars, n_before) in run.history.items()
    }
    with _modified_lake(
        run.source.lake,
        data_tickers(run.context),
        bars_by_ticker,
        run.interval,
        run.coarser,
    ) as lake:
        dataset = dataclasses.replace(run.context, lake=lake)
        report = run_backtest(run.strategy.strategy, dataset, run.window, lake=lake)
    return float(report.sharpe), float(report.max_drawdown)


class StressTest:
    """The 5th-percentile Sharpe over simulated validation windows must
    stay above -0.5, and the 95th-percentile drawdown within the limit."""

    id = "stress"

    class Options(BaseModel):
        model_config = ConfigDict(extra="forbid")

        n_paths: int = Field(default=200, ge=2)
        method: Literal["block_bootstrap", "garch_fhs"] = "block_bootstrap"
        block: float = Field(default=20.0, ge=1.0)
        source: Literal["full", "val"] = "full"
        min_p5_sharpe: float = -0.5
        max_drawdown_limit: float = Field(default=-0.3, le=0.0)
        max_workers: int | None = Field(default=None, ge=1)
        seed: int = 0

    def __init__(self, options: StressTest.Options | None = None) -> None:
        self.options = options or StressTest.Options()

    @classmethod
    def build(cls, options: StressTest.Options) -> StressTest:
        return cls(options)

    def run(self, strategy: Strategy, context: Any) -> SurvivalReport:
        o = self.options
        window = scoring_window(context, strategy, "val")
        interval = getattr(context, "interval", Interval.DAY_1)
        history = _history_bars(context, interval, window)
        paths = self._draw_paths(history, window)
        if not paths:
            return SurvivalReport(
                test_id=self.id,
                passed=False,
                metrics={"n_paths": 0.0},
                notes="insufficient data: no bars in the validation window",
            )
        results = run_tasks(
            _score_path,
            paths,
            payload=_PathRun(
                context=dataclasses.replace(context, lake=None),
                source=PortableLake(context.lake, data_tickers(context)),
                strategy=PortableStrategy(strategy),
                history=history,
                interval=interval,
                coarser=_coarser_intervals(context, interval),
                window=window,
            ),
            max_workers=o.max_workers,
        )
        sharpes = np.array([s for s, _ in results], dtype=float)
        drawdowns = np.array([d for _, d in results], dtype=float)
        finite = np.isfinite(sharpes)
        p5 = float(np.percentile(sharpes[finite], 5)) if finite.any() else math.nan
        dd95 = float(np.percentile(drawdowns, 5))  # the drawdown only 5% of paths beat
        metrics = {
            "n_paths": float(len(results)),
            "sharpe_p5": p5,
            "sharpe_median": float(np.median(sharpes[finite])) if finite.any() else math.nan,
            "max_dd_p95": dd95,
            "max_dd_median": float(np.median(drawdowns)),
        }
        failures = []
        if not p5 > o.min_p5_sharpe:
            failures.append(f"5th-percentile Sharpe {p5:.2f} <= {o.min_p5_sharpe:g}")
        if dd95 < o.max_drawdown_limit:
            failures.append(
                f"95th-percentile drawdown {dd95:.1%} beyond {o.max_drawdown_limit:.0%}"
            )
        return SurvivalReport(
            test_id=self.id,
            passed=not failures,
            metrics=metrics,
            notes="; ".join(failures) or f"{o.method}, {len(results)} paths",
        )

    def _draw_paths(
        self, history: dict[str, tuple[pd.DataFrame, int]], window: tuple[date, date]
    ) -> list[_Path]:
        o = self.options
        stamps = {
            t: pd.to_datetime(bars["timestamp"]).to_numpy() for t, (bars, _) in history.items()
        }
        window_ts = {t: stamps[t][n:] for t, (_, n) in history.items() if len(stamps[t]) > n}
        if not window_ts:
            return []
        timeline = np.unique(np.concatenate(list(window_ts.values())))
        if o.source == "val":
            source_ts = {t: window_ts.get(t, stamps[t][:0]) for t in stamps}
        else:
            source_ts = {t: s[1:] for t, s in stamps.items()}  # rows with a prior close
        pool = np.unique(np.concatenate([s for s in source_ts.values() if len(s)]))
        rng = np.random.default_rng(o.seed)
        fits = self._garch_fits(history, source_ts, stamps) if o.method == "garch_fhs" else {}
        paths = []
        for _ in range(o.n_paths):
            slots = stationary_bootstrap(len(pool), len(timeline), o.block, rng)
            drawn_ts = pool[slots]
            rows: dict[str, np.ndarray] = {}
            returns: dict[str, np.ndarray] = {}
            for ticker, own in window_ts.items():
                positions = np.searchsorted(timeline, own)
                picked = drawn_ts[positions]
                # the ticker's own row on the drawn day, else its latest one before
                local = np.searchsorted(stamps[ticker], picked, "right") - 1
                rows[ticker] = np.clip(local, 1, len(stamps[ticker]) - 1)
                if ticker in fits:
                    garch, resid_by_ts = fits[ticker]
                    shocks = np.array([resid_by_ts.get(ts, 0.0) for ts in picked], dtype=float)
                    returns[ticker] = garch.simulate(shocks)
            paths.append(_Path(rows=rows, returns=returns))
        return paths

    @staticmethod
    def _garch_fits(
        history: dict[str, tuple[pd.DataFrame, int]],
        source_ts: dict[str, np.ndarray],
        stamps: dict[str, np.ndarray],
    ) -> dict[str, tuple[GarchVol, dict[Any, float]]]:
        """Per ticker: a GARCH fit on its source returns (log close to
        close) and its standardised residual by timestamp. Tickers too
        short to fit fall back to the plain bootstrap."""
        fits: dict[str, tuple[GarchVol, dict[Any, float]]] = {}
        for ticker, (bars, n_before) in history.items():
            ret = _relatives(bars.reset_index(drop=True))["ret"]
            keep = np.isin(stamps[ticker], source_ts[ticker])
            keep[0] = False
            # fit on the history before the window so the simulation starts
            # from the state at the window's start
            fit_mask = keep.copy()
            fit_mask[n_before:] = False
            try:
                garch = GarchVol().fit(ret[fit_mask] if fit_mask.sum() >= 30 else ret[keep])
            except ValueError:
                _log.warning("stress.garch.skipped", ticker=ticker, rows=int(keep.sum()))
                continue
            resid = garch.standardize(ret[keep])
            fits[ticker] = (garch, dict(zip(stamps[ticker][keep], resid, strict=True)))
        return fits

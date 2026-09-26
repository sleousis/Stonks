"""Cross-instrument consistency test (BL-19, after Kaufman and Carver).

An edge that lives in one ticker is a story about that ticker, not a
strategy. This test backtests the tuned params on each ticker alone over
the validation window: every universe ticker, plus held-out tickers of the
same asset class(es) as the universe (``held_out`` names them;
``held_out_auto`` picks that many more from the lake's instruments, in id
order, among those with bars in the validation window). Each run rebuilds
and fits the strategy with the tuned params on a one-ticker dataset; a
strategy with a ``ticker`` parameter (single-ticker strategies) gets that
parameter set to the ticker under test.

Per ticker, P&L is the sum of its round trips' P&L (open lots marked at
the last close) and expectancy is the mean P&L per round trip. It passes
when:

- at least ``min_positive_share`` of the tickers have a positive
  validation expectancy (a ticker with no trade has none);
- the total P&L is positive and no ticker holds more than
  ``max_pnl_share`` of it.

Fewer than ``min_tickers`` tickers fails for insufficient data (RS-24):
there is nothing to be consistent across, and a pass would let every
single-ticker strategy skip the P4 check. The ``promotion`` preset sets
``held_out_auto`` to ``min_tickers``, so a small universe is topped up
with held-out tickers of the same class from the lake. With enough
tickers but no trade in any of them the test also fails for insufficient
data.

The per-ticker backtests run on the lab process pool (``lab.parallel``
via ``_reruns``); nothing is random, so the report does not depend on
``max_workers``.
"""

from __future__ import annotations

from typing import Any

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

from stonks.core.protocols import Strategy, SurvivalReport
from stonks.lab.survival._reruns import Rerun, RerunResult, run_reruns
from stonks.logging import get_logger

_log = get_logger("stonks.lab.survival.cross_instrument")

_DEFAULT_ASSET_CLASS = "equity"


class CrossInstrumentOptions(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    min_positive_share: float = Field(
        default=0.6,
        ge=0.5,
        le=0.8,
        description="Share of tickers that must make money per trade on average.",
    )
    max_pnl_share: float = Field(
        default=0.5,
        gt=0.0,
        le=1.0,
        description="Largest share of the total profit one ticker may hold.",
    )
    min_tickers: int = Field(
        default=3, ge=2, description="Fewest tickers to judge. Fewer fails for lack of data."
    )
    #: Extra tickers to test; any outside the universe's asset classes are
    #: dropped (and named in the notes).
    held_out: list[str] = Field(
        default_factory=list,
        description="Extra tickers to test that the strategy was not tuned on. Tickers of other asset classes are dropped.",
    )
    #: How many more same-class tickers to pick from the lake, in id order.
    held_out_auto: int = Field(
        default=0,
        ge=0,
        description="How many more tickers of the same asset class to pick from the stored data.",
    )
    #: Worker processes; ``None`` means ``lab.parallel.default_max_workers()``.
    max_workers: int | None = Field(default=None, ge=1)


class CrossInstrumentTest:
    id = "cross_instrument"
    Options = CrossInstrumentOptions

    def __init__(self, options: CrossInstrumentOptions | None = None, **overrides: Any) -> None:
        base = options or CrossInstrumentOptions()
        self.options = (
            CrossInstrumentOptions.model_validate({**base.model_dump(), **overrides})
            if overrides
            else base
        )

    @classmethod
    def build(cls, options: CrossInstrumentOptions) -> CrossInstrumentTest:
        return cls(options)

    def run(self, strategy: Strategy, context: Any) -> SurvivalReport:
        opts = self.options
        universe = list(context.universe)
        held_out, dropped = self._held_out(context, universe)
        tickers = universe + held_out
        notes: list[str] = []
        if held_out:
            notes.append(f"held out: {', '.join(held_out)}")
        if dropped:
            notes.append(f"dropped (other asset class): {', '.join(dropped)}")
        metrics: dict[str, float] = {
            "n_tickers": float(len(tickers)),
            "n_held_out": float(len(held_out)),
        }
        if len(tickers) < opts.min_tickers:
            notes.insert(0, f"insufficient data: {len(tickers)} tickers, {opts.min_tickers} needed")
            return SurvivalReport(self.id, False, metrics, "; ".join(notes))

        _log.info("cross_instrument.start", tickers=len(tickers), held_out=len(held_out))
        base_params = dict(getattr(strategy, "params", None) or {})
        has_ticker = any(s.name == "ticker" for s in type(strategy).parameter_spec())
        reruns = [
            Rerun(
                params={**base_params, "ticker": t} if has_ticker else base_params,
                universe=(t,),
            )
            for t in tickers
        ]
        results = run_reruns(
            strategy,
            context,
            reruns,
            max_workers=opts.max_workers,
            log_prefix="cross_instrument",
        )
        failed = [t for t, r in zip(tickers, results, strict=True) if not r.ok]
        if failed:
            notes.append(f"backtest failed (counted as no edge): {', '.join(failed)}")
        by_ticker: dict[str, RerunResult] = dict(zip(tickers, results, strict=True))
        n_trips = sum(r.n_round_trips for r in results if r.ok)
        metrics["n_round_trips"] = float(n_trips)
        if n_trips == 0:
            notes.insert(0, "insufficient data: no trades on any ticker in the validation window")
            return SurvivalReport(self.id, False, metrics, "; ".join(notes))

        positive = [
            t for t, r in by_ticker.items() if r.ok and r.n_round_trips and r.expectancy > 0
        ]
        positive_share = len(positive) / len(tickers)
        pnl = {t: (r.pnl if r.ok else 0.0) for t, r in by_ticker.items()}
        total = sum(pnl.values())
        top = max(pnl, key=lambda t: pnl[t])
        metrics["positive_share"] = positive_share
        metrics["total_pnl"] = total

        failures: list[str] = []
        if positive_share < opts.min_positive_share:
            failures.append(
                f"{len(positive)}/{len(tickers)} tickers with positive expectancy "
                f"({positive_share:.2f} < {opts.min_positive_share})"
            )
        if total <= 0:
            failures.append(f"total P&L {total:.2f} is not positive")
        else:
            share = pnl[top] / total
            metrics["max_pnl_share"] = share
            if share > opts.max_pnl_share:
                failures.append(
                    f"{top} holds {share:.0%} of total P&L (> {opts.max_pnl_share:.0%})"
                )
        notes.insert(0, "; ".join(failures) if failures else "edge holds across tickers")
        return SurvivalReport(self.id, not failures, metrics, "; ".join(notes))

    def _held_out(self, context: Any, universe: list[str]) -> tuple[list[str], list[str]]:
        """``(kept, dropped)`` held-out tickers (see module doc)."""
        opts = self.options
        explicit = [t for t in dict.fromkeys(opts.held_out) if t not in universe]
        if not explicit and opts.held_out_auto == 0:
            return [], []
        lake = context.lake
        classes = _asset_classes(lake, universe + explicit)
        wanted = {classes[t] for t in universe}
        kept = [t for t in explicit if classes[t] in wanted]
        dropped = [t for t in explicit if classes[t] not in wanted]
        if opts.held_out_auto:
            start, end = context.val_window
            frame = lake.sql(
                """
                SELECT i.id
                  FROM instruments i
                 WHERE COALESCE(i.asset_class, ?) = ANY(?)
                   AND NOT (i.id = ANY(?))
                   AND EXISTS (
                        SELECT 1 FROM bars b
                         WHERE b.ticker = i.id AND b.interval = ?
                           AND CAST(b.timestamp AS DATE) BETWEEN ? AND ?)
                 ORDER BY i.id
                """,
                [
                    _DEFAULT_ASSET_CLASS,
                    sorted(wanted),
                    universe + explicit,
                    context.interval.code,
                    start,
                    end,
                ],
            )
            kept += list(frame["id"])[: opts.held_out_auto]
        return kept, dropped


def _asset_classes(lake: Any, tickers: list[str]) -> dict[str, str]:
    """Asset class per ticker; a missing instrument row or NULL is equity
    (the engine's convention)."""
    frame: pd.DataFrame = lake.sql(
        "SELECT id, asset_class FROM instruments WHERE id = ANY(?)", [tickers]
    )
    found = {
        row.id: row.asset_class or _DEFAULT_ASSET_CLASS for row in frame.itertuples(index=False)
    }
    return {t: found.get(t, _DEFAULT_ASSET_CLASS) for t in tickers}

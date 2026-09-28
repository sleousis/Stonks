"""The factor bench (roadmap 23.13): score every factor on one universe and
window, and say which still work.

For each factor the bench computes the IC at one horizon on sampled dates
(the tear sheet's convention: forward returns from the next open, read only
inside the window, P12), its Newey-West t-stat and a two-sided p-value. The
p-values of all factors go through Benjamini-Hochberg at ``q`` (P2, many
tests at once), and each factor gets a label:

- ``alive``: significant after FDR, with the sign its hypothesis predicts;
- ``reversed``: significant after FDR, with the opposite sign;
- ``dead``: not significant after FDR;
- ``n/a``: no IC at all (no values, too few names).

For a published factor the bench also reports the IC after publication, so
decay is visible next to the verdict.

Every factor benched is one trial in the trial ledger (:mod:`stonks.lab.trials`),
all in the research family ``factor_bench``, so the count of factors ever
looked at survives across runs and later tests can deflate for it.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Literal

import pandas as pd

from stonks.factors.base import Factor
from stonks.factors.engine import PanelRequest
from stonks.factors.panels import FactorEngine
from stonks.factors.tearsheet import (
    TearSheetOptions,
    _clean,
    _hac_lags,
    _horizon_summary,
    _label_panels,
    _periods,
)
from stonks.lab.signal_eval import MIN_UNIVERSE
from stonks.lab.trials import LabRunSpec, TrialLedger, TrialRecord
from stonks.logging import get_logger
from stonks.stats.multiple_testing import benjamini_hochberg

__all__ = ["BENCH_FAMILY", "BenchResult", "BenchRow", "factor_bench", "record_bench"]

_log = get_logger("stonks.factors.bench")

#: The trial-ledger family every bench run belongs to.
BENCH_FAMILY = "factor_bench"
BENCH_CLASS = "stonks.factors.bench:FactorBench"

Label = Literal["alive", "reversed", "dead", "n/a"]


@dataclass(frozen=True)
class BenchRow:
    factor_id: str
    family: str
    direction: int
    n_dates: int
    mean_ic: float
    t_stat_hac: float
    #: Two-sided, from the HAC t-stat on a normal.
    p_value: float
    label: Label
    #: Mean IC after publication, for a factor with a source paper.
    post_publication_ic: float | None = None


@dataclass(frozen=True)
class BenchResult:
    window: tuple[str, str]
    interval: str
    universe_id: str | None
    n_tickers: int
    horizon: int
    every_bars: int
    #: The false discovery rate.
    q: float
    rows: list[BenchRow] = field(default_factory=list)
    status: str = "ok"
    note: str = ""
    #: The trial-ledger run, once recorded.
    run_id: str | None = None
    #: Factors benched in every run so far, this one included.
    n_trials_family: int = 0

    def counts(self) -> dict[str, int]:
        out = {"alive": 0, "reversed": 0, "dead": 0, "n/a": 0}
        for row in self.rows:
            out[row.label] += 1
        return out

    def to_dict(self) -> dict[str, Any]:
        return _clean(asdict(self) | {"counts": self.counts()})


def two_sided_p(t_stat: float) -> float:
    """The two-sided p-value of ``t_stat`` on a standard normal; NaN stays NaN."""
    if math.isnan(t_stat):
        return math.nan
    return math.erfc(abs(t_stat) / math.sqrt(2.0))


def label_rows(
    stats: Sequence[tuple[Factor, int, float, float, float | None]], q: float
) -> list[BenchRow]:
    """Label ``(factor, n_dates, mean_ic, t_stat, post_publication_ic)`` rows
    with Benjamini-Hochberg over the factors that have a p-value."""
    p = [two_sided_p(t) for _, _, _, t, _ in stats]
    tested = [i for i, v in enumerate(p) if math.isfinite(v)]
    reject = benjamini_hochberg([p[i] for i in tested], q=q)
    significant = {i for i, r in zip(tested, reject, strict=True) if r}
    rows = []
    for i, (factor, n, ic, t, post) in enumerate(stats):
        label: Label
        if i not in tested:
            label = "n/a"
        elif i in significant:
            label = "alive" if ic * factor.direction > 0 else "reversed"
        else:
            label = "dead"
        rows.append(
            BenchRow(
                factor_id=factor.id,
                family=factor.family,
                direction=factor.direction,
                n_dates=n,
                mean_ic=ic,
                t_stat_hac=t,
                p_value=p[i],
                label=label,
                post_publication_ic=post,
            )
        )
    return rows


def factor_bench(
    factors: Sequence[Factor],
    lake: Any,
    request: PanelRequest,
    *,
    horizon: int = 21,
    every_bars: int = 5,
    q: float = 0.1,
    min_names: int = 5,
    engine: FactorEngine | None = None,
) -> BenchResult:
    """Score ``factors`` over ``request`` and label each (module doc)."""
    if not 0 < q < 1:
        raise ValueError("q must be between 0 and 1")
    opts = TearSheetOptions(horizons=(horizon,), every_bars=every_bars, min_names=min_names)
    base: dict[str, Any] = {
        "window": (request.start.isoformat(), request.end.isoformat()),
        "interval": request.interval.code,
        "universe_id": request.universe_id,
        "n_tickers": len(request.universe),
        "horizon": horizon,
        "every_bars": every_bars,
        "q": q,
    }
    if len(request.universe) < MIN_UNIVERSE:
        note = f"needs at least {MIN_UNIVERSE} tickers, got {len(request.universe)}"
        return BenchResult(**base, status="n/a", note=note)
    engine = engine or FactorEngine(lake)
    labels = _label_panels(lake, request, sorted({horizon, every_bars}))
    sampled = pd.DatetimeIndex(labels[every_bars].index[::every_bars])
    if sampled.empty:
        return BenchResult(**base, status="n/a", note="no bars in the window")
    fwd = labels[horizon].reindex(sampled)
    lags = _hac_lags(horizon, every_bars)
    stats: list[tuple[Factor, int, float, float, float | None]] = []
    for factor in factors:
        scores = engine.panel(factor, request, dates=sampled).reindex(
            index=sampled, columns=list(request.universe)
        )
        summary, ic = _horizon_summary(horizon, scores, fwd, opts)
        post = next(
            (
                p.mean_ic
                for p in _periods(factor, scores, fwd, ic, opts, lags)
                if p.period == "post_publication"
            ),
            None,
        )
        stats.append((factor, summary.n_dates, summary.mean_ic, summary.t_stat_hac, post))
        _log.debug("factor.bench.scored", factor=factor.id, ic=summary.mean_ic)
    return BenchResult(**base, rows=label_rows(stats, q))


def record_bench(ledger: TrialLedger, result: BenchResult) -> BenchResult:
    """Count every benched factor as one trial in the ``factor_bench``
    family. The score is the IC turned by the factor's direction."""
    spec = LabRunSpec(
        strategy_class=BENCH_CLASS,
        hypothesis="each factor ranks forward returns in the direction it states",
        tuner="factor_bench",
        objective=f"ic_h{result.horizon}",
        budget=len(result.rows),
        dataset={
            "window": list(result.window),
            "interval": result.interval,
            "universe_id": result.universe_id,
            "n_tickers": result.n_tickers,
            "q": result.q,
        },
        family=BENCH_FAMILY,
    )
    trials = [
        TrialRecord(
            trial_index=i,
            params={"factor": row.factor_id, "label": row.label},
            score=row.mean_ic * row.direction,
            n_bars=row.n_dates,
        )
        for i, row in enumerate(result.rows)
    ]
    verdict = "pass" if any(r.label == "alive" for r in result.rows) else "fail"
    run_id = ledger.record_run(spec, trials, verdict=verdict)
    total = ledger.n_trials_family(BENCH_FAMILY)
    return replace(result, run_id=run_id, n_trials_family=total)

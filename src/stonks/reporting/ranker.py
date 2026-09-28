"""Learning ranker section (roadmap 23.12): the fitted model's settings,
its out-of-fold IC from purged folds, the IC by month, and each feature's
permutation importance. Same escaping rule and stylesheet as the other
reports (``reporting.html``).

A strategy (or the strategy inside a wrapper) with a ``ranker_report()``
method that returns a :class:`~stonks.features.ranking.RankerReport` gets
the section (:func:`ranker_sections`).
"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Any, Protocol, runtime_checkable

from stonks.features.ranking import RankerReport
from stonks.reporting.html import e, table, tile

__all__ = ["ReportsRanker", "ranker_sections", "render_ranker_section"]

#: Features listed in the importance table.
TOP_FEATURES = 20
_MAX_WRAPPER_DEPTH = 5


@runtime_checkable
class ReportsRanker(Protocol):
    def ranker_report(self) -> RankerReport | None: ...


def _f(x: float | None, fmt: str) -> str:
    return "-" if x is None or not math.isfinite(x) else fmt.format(x)


def _monthly(ic_by_date: dict[str, float]) -> list[tuple[str, float, int]]:
    """``(YYYY-MM, mean IC, dates)`` per month, in order."""
    buckets: dict[str, list[float]] = defaultdict(list)
    for day, value in ic_by_date.items():
        if math.isfinite(value):
            buckets[day[:7]].append(value)
    return [(m, sum(v) / len(v), len(v)) for m, v in sorted(buckets.items())]


def render_ranker_section(report: RankerReport) -> str:
    """One ``<section>`` for a fitted ranker."""
    s = report.summary
    tiles = "".join(
        [
            tile("Mean IC", _f(s.get("ic_mean"), "{:+.3f}")),
            tile("IC IR", _f(s.get("ic_ir"), "{:+.2f}")),
            tile("IC above zero", _f(s.get("ic_hit_rate"), "{:.0%}")),
            tile("Rows", f"{report.n_rows:,}"),
        ]
    )
    meta = (
        f"{report.model} · {len(report.feature_names)} features · label {report.horizon_bars} bars "
        f"ahead · trained {report.train_start or '-'} to {report.train_end or '-'} · "
        f"{report.n_dates} dates · {report.cv_folds} purged folds"
    )
    params = ", ".join(f"{k} {v}" for k, v in sorted(report.hyperparameters.items()))
    ranked = sorted(report.importance.items(), key=lambda kv: (-kv[1], kv[0]))[:TOP_FEATURES]
    importance = table(
        ["feature", "importance (IC drop)"],
        ([e(name), e(_f(value, "{:+.4f}"))] for name, value in ranked),
        "no out-of-fold importance (the diagnostic was off)",
    )
    months = table(
        ["month", "mean IC", "dates"],
        ([e(m), e(_f(v, "{:+.3f}")), e(str(n))] for m, v, n in _monthly(report.ic_by_date)),
        "no out-of-fold IC",
    )
    return (
        '<section class="ranker"><h2>Learning ranker</h2>'
        '<p class="muted">Scored on purged, embargoed folds of the training window only. '
        "Importance is the drop in the fold's IC when a feature is shuffled. Nothing was "
        "picked from these numbers.</p>"
        f'<p class="muted">{e(meta)}</p><p class="muted">{e(params)}</p>'
        f'<div class="tiles">{tiles}</div>'
        f"<h3>Top features</h3>{importance}<h3>IC by month</h3>{months}</section>"
    )


def ranker_sections(strategy: Any) -> list[str]:
    """The ranker section for ``strategy`` (or the one inside a wrapper),
    empty when it is not a fitted ranker."""
    current = strategy
    for _ in range(_MAX_WRAPPER_DEPTH):
        if isinstance(current, ReportsRanker):
            report = current.ranker_report()
            return [render_ranker_section(report)] if report is not None else []
        current = getattr(current, "inner", None)
        if current is None:
            break
    return []

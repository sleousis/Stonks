"""HTML section for feature importance under purged CV (roadmap 23.10).
Same escaping rule and stylesheet as the other reports (``reporting.html``);
no scripts, no external assets."""

from __future__ import annotations

import math
from typing import Any

from stonks.reporting.html import CSS, e, table, tile

_TITLES = {"mda": "MDA", "sfi": "SFI", "clustered_mda": "Clustered MDA"}
_NOTES = {
    "mda": "Drop in the out-of-fold score when the feature is shuffled.",
    "sfi": "Out-of-fold score of a model on the feature alone.",
    "clustered_mda": "Drop when every feature of the cluster is shuffled together.",
}


def _f(x: float, fmt: str = "{:+.4f}") -> str:
    return "-" if not math.isfinite(x) else fmt.format(x)


def render_importance_section(report: Any) -> str:
    """``ImportanceReport`` as one ``<section>``."""
    tiles = "".join(
        [
            tile("Samples", str(report.n_samples)),
            tile("Purged folds", str(report.folds)),
            tile("Embargo", f"{report.embargo_pct:.1%}"),
            tile("Score", report.scoring),
        ]
    )
    parts = []
    for t in report.tables:
        rows = (
            [
                e(r.name),
                e(_f(r.mean)),
                e(_f(r.std_err, "{:.4f}")),
                e(", ".join(r.members)) if len(r.members) > 1 else "",
            ]
            for r in t.rows
        )
        parts.append(
            f"<h3>{e(_TITLES.get(t.method, t.method))}</h3>"
            f'<p class="muted">{e(_NOTES.get(t.method, ""))}</p>'
            + table(["feature", "mean", "std err", "members"], rows, "no features")
        )
    return (
        f"<section><h2>Feature importance · {e(report.strategy_id)}</h2>"
        '<p class="muted">Training window only. Dropping features after reading this '
        "is a new hypothesis and a new lab run.</p>"
        f'<div class="tiles">{tiles}</div>{"".join(parts)}</section>'
    )


def render_importance_page(report: Any) -> str:
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>Feature importance · {e(report.strategy_id)}</title><style>{CSS}</style></head>"
        "<body><header><h1>Feature importance</h1></header>"
        f"<main>{render_importance_section(report)}</main></body></html>"
    )

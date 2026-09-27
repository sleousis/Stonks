"""Factor attribution of P&L (roadmap 22.4).

How much of a backtest's return came from the market and each style
(momentum, size, value, volatility, sector) and how much was its own.

The book's per-bar returns ``r_t`` are regressed on the style factor
returns ``f_t`` of its universe (:func:`stonks.factors.style.style_factor_returns`)
over the whole window:

    r_t = a + sum_k b_k f_kt + e_t

A factor's contribution is ``b_k * sum_t f_kt``. What the factors leave,
``sum_t r_t - sum_k contribution_k``, is the specific part (``n * a``: the
residuals sum to zero). Returns are summed, not compounded, so the parts
add up to the total exactly. Sector dummies are shown as one ``sector``
line.

This is a report after the fact. The betas use the whole window, which is
fine for explaining P&L and never feeds a decision. The factor returns
themselves are point in time (each bar's exposures are the ones known the
bar before).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd

from stonks.reporting.charts import line_chart
from stonks.reporting.html import e, table

__all__ = [
    "FactorAttribution",
    "attribute_returns",
    "render_factor_attribution_section",
    "returns_from_curve",
]

#: Bars needed before a regression means anything.
MIN_BARS = 20
SECTOR_PREFIX = "sector:"
SECTOR = "sector"


@dataclass(frozen=True)
class FactorAttribution:
    """Where a return came from (module doc)."""

    #: Beta per factor (sector dummies kept apart here).
    betas: dict[str, float]
    #: Summed return per factor line (sector dummies grouped).
    contributions: dict[str, float]
    #: What the factors do not explain.
    specific: float
    #: Sum of the book's per-bar returns.
    total: float
    r_squared: float
    n_bars: int
    #: Running sums of the factor part and the specific part.
    cumulative_factor: list[tuple[date, float]] = field(default_factory=list)
    cumulative_specific: list[tuple[date, float]] = field(default_factory=list)

    @property
    def factor_total(self) -> float:
        return float(sum(self.contributions.values()))


def returns_from_curve(dates: Sequence[date], curve: Sequence[float]) -> pd.Series:
    """Simple per-bar returns of an equity curve, indexed by the bar date."""
    values = pd.Series(list(curve), index=pd.DatetimeIndex(pd.to_datetime(list(dates))))
    values = pd.Series(values[~values.index.duplicated(keep="last")], dtype=float)
    return values.pct_change().iloc[1:].replace([np.inf, -np.inf], np.nan).dropna()


def _days(index: pd.Index) -> pd.DatetimeIndex:
    return pd.DatetimeIndex(pd.to_datetime(index).to_numpy(dtype="datetime64[D]"))


def _group(name: str) -> str:
    return SECTOR if name.startswith(SECTOR_PREFIX) else name


def attribute_returns(
    portfolio_returns: pd.Series, factor_returns: pd.DataFrame, *, min_bars: int = MIN_BARS
) -> FactorAttribution | None:
    """The attribution of ``portfolio_returns`` (index dates) to
    ``factor_returns`` (dates by factors), on the dates both have. ``None``
    with fewer than ``min_bars`` such dates."""
    if factor_returns is None or factor_returns.empty or portfolio_returns.empty:
        return None
    r = portfolio_returns.copy()
    r.index = _days(r.index)
    f = factor_returns.copy()
    f.index = _days(f.index)
    f = f.fillna(0.0)
    joined = pd.concat([r.rename("__book__"), f], axis=1, join="inner").dropna()
    if len(joined) < max(min_bars, f.shape[1] + 2):
        return None
    y = joined.pop("__book__").to_numpy(dtype=float)
    x = joined.to_numpy(dtype=float)
    design = np.column_stack([np.ones(len(y)), x])
    coef, *_ = np.linalg.lstsq(design, y, rcond=None)
    betas = dict(zip(joined.columns, coef[1:].tolist(), strict=True))
    fitted = design @ coef
    ss_tot = float(((y - y.mean()) ** 2).sum())
    r_squared = 1.0 - float(((y - fitted) ** 2).sum()) / ss_tot if ss_tot > 0 else 0.0
    per_bar = x * coef[1:]
    contributions: dict[str, float] = {}
    for j, name in enumerate(joined.columns):
        key = _group(str(name))
        contributions[key] = contributions.get(key, 0.0) + float(per_bar[:, j].sum())
    total = float(y.sum())
    factor_path = np.cumsum(per_bar.sum(axis=1))
    specific_path = np.cumsum(y) - factor_path
    days = [d.date() for d in joined.index]
    return FactorAttribution(
        betas={str(k): float(v) for k, v in betas.items()},
        contributions=contributions,
        specific=total - float(sum(contributions.values())),
        total=total,
        r_squared=r_squared,
        n_bars=len(y),
        cumulative_factor=list(zip(days, factor_path.tolist(), strict=True)),
        cumulative_specific=list(zip(days, specific_path.tolist(), strict=True)),
    )


def _p(x: float) -> str:
    return "-" if not math.isfinite(x) else f"{x:+.2%}"


def render_factor_attribution_section(attr: FactorAttribution | None, title: str = "") -> str:
    """One ``<section>``: a table of contributions and betas and a chart of
    the factor and specific parts over time. Empty for ``None``."""
    if attr is None:
        return ""
    grouped_betas: dict[str, list[float]] = {}
    for name, beta in attr.betas.items():
        grouped_betas.setdefault(_group(name), []).append(beta)
    order = sorted(attr.contributions, key=lambda k: -abs(attr.contributions[k]))
    rows = [
        [
            e(name),
            e(_p(attr.contributions[name])),
            e(
                f"{grouped_betas[name][0]:+.2f}"
                if len(grouped_betas.get(name, [])) == 1
                else f"{len(grouped_betas.get(name, []))} sectors"
            ),
        ]
        for name in order
    ]
    rows.append([e("specific"), e(_p(attr.specific)), e("-")])
    rows.append([e("total"), e(_p(attr.total)), e("-")])
    chart = line_chart(
        [
            ("factors", attr.cumulative_factor, "s1"),
            ("specific", attr.cumulative_specific, "s2"),
        ],
        title=f"Factor and specific P&L{': ' + title if title else ''}",
        fmt=lambda v: f"{v:.1%}",
    )
    note = (
        f"Per-bar returns regressed on the universe's style factor returns over "
        f"{attr.n_bars} bars (R squared {attr.r_squared:.2f}). Returns are summed, so "
        "the lines add up to the total."
    )
    body = table(["factor", "contribution", "beta"], rows, "no factors")
    return (
        '<section class="factor-attribution"><h2>Factor attribution</h2>'
        f'<p class="muted">{e(note)}</p>{body}{chart}</section>'
    )

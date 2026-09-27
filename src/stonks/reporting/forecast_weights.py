"""Forecast weights section (roadmap 22.7): for each instrument, each
rule's weight, turnover, cost in Sharpe units and whether the speed limit
dropped it. Same escaping rule and stylesheet as the other reports
(``reporting.html``).

:func:`strategy_report_sections` is how a backtest tear sheet finds the
section: any strategy (or the inner strategy of a wrapper) with a
``forecast_weight_fits()`` method gets one.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from stonks.features.forecast_weights import ForecastWeightFit
from stonks.reporting.html import badge, e, table

__all__ = [
    "ReportsForecastWeights",
    "render_forecast_weights_section",
    "strategy_report_sections",
]

#: How deep to look through wrappers for an inner strategy.
_MAX_WRAPPER_DEPTH = 5


@runtime_checkable
class ReportsForecastWeights(Protocol):
    def forecast_weight_fits(self) -> Mapping[str, ForecastWeightFit]: ...


def _f(x: float | None, fmt: str) -> str:
    return "-" if x is None or not math.isfinite(x) else fmt.format(x)


def _ticker_html(ticker: str, fit: ForecastWeightFit) -> str:
    meta = (
        f"{fit.method} · {fit.status} · fit through {fit.fit_end} · {fit.n_obs} rows · "
        f"FDM {_f(fit.fdm, '{:.2f}')} · cost {_f(fit.cost * 10_000, '{:.1f}')} bps a trade · "
        f"vol {_f(fit.sigma_annual, '{:.1%}')} a year · "
        f"speed limit {_f(fit.max_cost_sr, '{:.3f}')} SR"
    )
    rows = (
        [
            e(r.name),
            e(_f(r.weight, "{:.1%}")),
            e(_f(r.scalar, "{:.2f}")),
            e(_f(r.turnover, "{:.1f}")),
            e(_f(r.cost_sr, "{:.3f}")),
            e(_f(r.gross_sr, "{:+.2f}")),
            e(_f(r.net_sr, "{:+.2f}")),
            badge(not r.dropped, "kept", "dropped") + (f" {e(r.reason)}" if r.reason else ""),
        ]
        for r in fit.rules
    )
    body = table(
        [
            "rule",
            "weight",
            "scalar",
            "turnover a year",
            "cost (SR a year)",
            "SR before costs",
            "SR after costs",
            "status",
        ],
        rows,
        "no rules",
    )
    return f'<h3>{e(ticker)}</h3><p class="muted">{e(meta)}</p>{body}'


def render_forecast_weights_section(fits: Mapping[str, ForecastWeightFit]) -> str:
    """One ``<section>`` for the latest fit of each ticker; empty without
    fits."""
    if not fits:
        return ""
    parts = "".join(_ticker_html(t, fits[t]) for t in sorted(fits))
    return (
        '<section class="forecast-weights"><h2>Forecast weights</h2>'
        '<p class="muted">Each rule\'s weight in the combined forecast, fitted on bars '
        "before the current period only. A rule whose cost is above the speed limit is "
        "dropped for that instrument.</p>"
        f"{parts}</section>"
    )


def strategy_report_sections(strategy: Any) -> list[str]:
    """Extra tear sheet sections for ``strategy`` (or the strategy inside
    a wrapper): today the forecast weights of a :class:`ReportsForecastWeights`."""
    current = strategy
    for _ in range(_MAX_WRAPPER_DEPTH):
        if isinstance(current, ReportsForecastWeights):
            section = render_forecast_weights_section(dict(current.forecast_weight_fits()))
            return [section] if section else []
        current = getattr(current, "inner", None)
        if current is None:
            break
    return []

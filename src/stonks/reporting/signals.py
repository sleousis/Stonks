"""HTML sections for signal research: IC analysis (BL-33) and the event
study (BL-34). Same escaping rule and stylesheet as the other reports
(``reporting.html``); no scripts, no external assets."""

from __future__ import annotations

import math
from typing import Any

from stonks.reporting.html import CSS, badge, e, table, tile


def _f(x: float | None, fmt: str = "{:+.4f}") -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "-"
    if isinstance(x, float) and math.isinf(x):
        return "+∞" if x > 0 else "-∞"
    return fmt.format(x)


def _p(x: float | None) -> str:
    return _f(x, "{:+.3%}")


def render_signal_ic_section(result: Any) -> str:
    """``SignalICResult`` as one ``<section>``."""
    head = (
        f"<h2>Signal IC · {e(result.strategy_id)}</h2>"
        f'<p class="muted">{e(result.window[0])} to {e(result.window[1])} · '
        f"{e(result.n_tickers)} tickers · {e(result.n_dates)} dates sampled every "
        f"{e(result.every_bars)} bars · returns from the next open</p>"
    )
    if result.status != "ok":
        return f'<section>{head}<p class="muted">{e(result.status)}: {e(result.note)}</p></section>'
    tiles = "".join(
        [
            tile(f"IC estimate (h={result.ic_horizon})", _f(result.ic_estimate)),
            tile("Score turnover", _f(result.score_turnover, "{:.3f}")),
            tile("Top-bucket turnover", _f(result.top_quantile_turnover, "{:.3f}")),
        ]
    )
    rows = (
        [
            e(h.horizon),
            e(h.n_dates),
            e(_f(h.mean_ic)),
            e(_f(h.ic_std, "{:.4f}")),
            e(_f(h.icir, "{:+.3f}")),
            e(_f(h.hit_rate, "{:.1%}")),
            e(_f(h.t_stat_hac, "{:+.2f}")),
            e(h.hac_lags),
            e(_p(h.spread_mean)),
            e(_f(h.spread_t_hac, "{:+.2f}")),
        ]
        for h in result.horizons
    )
    ic_table = table(
        [
            "horizon",
            "dates",
            "mean IC",
            "IC std",
            "ICIR",
            "IC > 0",
            "t (HAC)",
            "HAC lags",
            "top-bottom",
            "t (HAC)",
        ],
        rows,
        "no horizons",
    )
    quantiles = table(
        ["horizon", *(f"Q{k + 1}" for k in range(result.n_quantiles))],
        ([e(h.horizon), *(e(_p(v)) for v in h.quantile_means)] for h in result.horizons),
        "no horizons",
    )
    return (
        f'<section>{head}<div class="tiles">{tiles}</div>'
        f"<h3>IC by horizon (decay)</h3>{ic_table}"
        f"<h3>Mean forward return by score bucket (Q1 lowest)</h3>{quantiles}</section>"
    )


def render_event_study_section(result: Any) -> str:
    """``EventStudyResult`` as one ``<section>``."""
    h_hold = result.holding_bars
    rows = []
    for g in result.groups:
        for h in g.horizons:
            ok = h.excess > 0 and h.p_value <= result.alpha
            verdict = badge(ok) if h.horizon == h_hold else ""
            rows.append(
                [
                    e(g.asset_class),
                    e(h.horizon),
                    e(h.n_events),
                    e(_p(h.event_mean)),
                    e(_p(h.baseline_mean)),
                    e(_p(h.excess)),
                    f"{e(_p(h.ci_low))} … {e(_p(h.ci_high))}",
                    e(_f(h.p_value, "{:.3f}")),
                    verdict,
                ]
            )
    body = table(
        [
            "group",
            "horizon",
            "events",
            "event mean",
            "baseline",
            "excess",
            f"{1 - result.alpha:.0%} CI",
            "p",
            "at holding",
        ],
        rows,
        "no events",
    )
    return (
        f"<section><h2>Event study · {e(result.strategy_id)}</h2>"
        f'<p class="muted">{e(result.window[0])} to {e(result.window[1])} · '
        f"{e(result.n_events)} entries · holding {e(h_hold)} bars ({e(result.holding_source)})"
        f" · block bootstrap, {e(result.n_boot)} draws, seed {e(result.seed)}</p>{body}</section>"
    )


def render_signal_page(ic: Any, events: Any = None) -> str:
    """A self-contained page with the IC section and, when given, the
    event-study section."""
    sections = render_signal_ic_section(ic)
    if events is not None:
        sections += render_event_study_section(events)
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>Signal research · {e(ic.strategy_id)}</title><style>{CSS}</style></head>"
        f"<body><header><h1>Signal research</h1></header><main>{sections}</main></body></html>"
    )

"""Factor tear sheets (roadmap 22.3), in the style of alphalens.

For any :class:`~stonks.factors.base.Factor` over a universe and window:

- **Forward returns** from the next open, ``O[t+1+h] / O[t+1] - 1``
  (:func:`~stonks.factors.expression.next_open_label`), read only inside
  the window: a date without ``1 + h`` later bars has no return (P12). The
  same convention as :mod:`stonks.lab.signal_eval`.
- **IC per horizon**: the Spearman correlation of factor values and forward
  returns across names on each sampled date, then mean, ICIR, hit rate and
  a Newey-West t-stat with ``ceil(h / k) - 1`` lags.
- **IC by group** at the main horizon: within each sector, asset class and
  size bucket. Size is the market cap known on each date when the lake has
  it for most names, else the trailing 20-bar mean dollar volume. Sector and
  asset class are today's classification, not point in time.
- **Returns per quantile**: mean forward return of each equal-count bucket
  per horizon, the top-minus-bottom spread, and cumulative returns of each
  bucket held ``every_bars`` bars at a time.
- **Factor alpha and beta**: the long-short book (top minus bottom bucket,
  turned by the factor's ``direction``) regressed on the equal-weight
  universe return over the same non-overlapping periods.
- **Monthly IC heatmap**: mean IC per calendar month at the main horizon.
- **Turnover**: one minus the rank autocorrelation of factor values, and
  the share of the top bucket replaced between sampled dates.
- **Periods** (roadmap 23.13): for a factor with a
  :class:`~stonks.factors.base.Provenance`, the IC and top-minus-bottom
  spread at the main horizon split into the paper's sample, the years after
  it and before publication, and the years after publication.

Values are reported raw: a factor with ``direction = -1`` shows a negative
IC when it works. Only the alpha book uses the direction.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from typing import Any, Literal, cast, get_args

import numpy as np
import pandas as pd

from stonks.core.timeutil import day_end, day_start
from stonks.core.types import AssetClass
from stonks.factors.base import Factor
from stonks.factors.engine import PanelRequest, evaluate, prepare_bars, read_bars
from stonks.factors.expression import next_open_label
from stonks.factors.panels import FactorEngine
from stonks.features.volatility import periods_per_year
from stonks.lab.signal_eval import (
    MIN_UNIVERSE,
    _mean_se,
    _quantile_returns,
    _ratio,
    _turnover,
    ic_by_date,
)
from stonks.logging import get_logger
from stonks.store.corporate_actions import LakeCorporateActions

__all__ = [
    "AlphaBeta",
    "FactorTearSheet",
    "GroupIC",
    "HorizonSummary",
    "MonthlyIC",
    "PeriodIC",
    "QuantileCurves",
    "TearSheetOptions",
    "factor_tearsheet",
]

_log = get_logger("stonks.factors.tearsheet")

SizeBasis = Literal["market_cap", "dollar_volume", "none"]
_DOLLAR_VOLUME_BARS = 20
_SIZE_LABELS = {2: ("small", "large"), 3: ("small", "mid", "large")}


@dataclass(frozen=True)
class TearSheetOptions:
    #: Forward-return horizons in bars.
    horizons: tuple[int, ...] = (1, 5, 21)
    #: Sample every this many bars of the window.
    every_bars: int = 5
    n_quantiles: int = 5
    #: Fewest names with a value and a return for a date's IC.
    min_names: int = 5
    #: Size buckets for the IC by size.
    size_buckets: int = 3

    def __post_init__(self) -> None:
        horizons = tuple(sorted({int(h) for h in self.horizons}))
        if not horizons or horizons[0] < 1:
            raise ValueError("every horizon must be at least 1 bar")
        object.__setattr__(self, "horizons", horizons)
        if self.every_bars < 1:
            raise ValueError("every_bars must be at least 1")
        if self.n_quantiles < 2:
            raise ValueError("n_quantiles must be at least 2 quantiles")
        if self.min_names < 2:
            raise ValueError("min_names must be at least 2")
        if self.size_buckets < 2:
            raise ValueError("size_buckets must be at least 2")


@dataclass(frozen=True)
class HorizonSummary:
    horizon: int
    n_dates: int
    mean_ic: float
    ic_std: float
    icir: float
    #: Share of dates with a positive IC.
    hit_rate: float
    t_stat_hac: float
    hac_lags: int
    #: Mean forward return per bucket, lowest values first.
    quantile_means: list[float]
    spread_mean: float
    spread_t_hac: float


@dataclass(frozen=True)
class GroupIC:
    group: str
    n_dates: int
    #: Mean names per date with a value and a return.
    mean_names: float
    mean_ic: float
    t_stat_hac: float


@dataclass(frozen=True)
class QuantileCurves:
    """Cumulative return of each bucket, held ``every_bars`` bars at a time."""

    dates: list[str] = field(default_factory=list)
    #: One list per bucket, lowest values first.
    series: list[list[float]] = field(default_factory=list)
    #: Top minus bottom, cumulative.
    spread: list[float] = field(default_factory=list)


@dataclass(frozen=True)
class AlphaBeta:
    """The long-short book regressed on the equal-weight universe."""

    n_periods: int = 0
    alpha_annual: float = math.nan
    alpha_t: float = math.nan
    beta: float = math.nan
    r_squared: float = math.nan
    benchmark: str = "equal_weight"


@dataclass(frozen=True)
class MonthlyIC:
    year: int
    #: January to December; ``None`` where the month has no IC.
    months: list[float | None]


@dataclass(frozen=True)
class PeriodIC:
    """The main-horizon IC over one period of a published factor's life."""

    #: ``pre_sample``, ``in_sample``, ``post_sample`` or ``post_publication``.
    period: str
    #: First and last sampled date in the period.
    start: str
    end: str
    n_dates: int
    mean_ic: float
    t_stat_hac: float
    spread_mean: float


@dataclass(frozen=True)
class FactorTearSheet:
    factor: dict[str, Any]
    window: tuple[str, str]
    interval: str
    universe_id: str | None
    n_tickers: int
    n_dates: int
    every_bars: int
    n_quantiles: int
    #: ``ok`` or ``n/a`` (see ``note``).
    status: str
    note: str = ""
    #: Mean share of the universe with a value on a sampled date.
    coverage: float = math.nan
    horizons: list[HorizonSummary] = field(default_factory=list)
    ic_horizon: int | None = None
    #: ``(date, IC)`` at ``ic_horizon`` for dates with an IC.
    ic_series: list[tuple[str, float]] = field(default_factory=list)
    ic_by_group: dict[str, list[GroupIC]] = field(default_factory=dict)
    size_basis: SizeBasis = "none"
    quantile_curves: QuantileCurves = field(default_factory=QuantileCurves)
    alpha_beta: AlphaBeta = field(default_factory=AlphaBeta)
    monthly_ic: list[MonthlyIC] = field(default_factory=list)
    score_turnover: float = math.nan
    top_quantile_turnover: float = math.nan
    #: The IC by period against the factor's paper; empty without one.
    periods: list[PeriodIC] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Plain JSON types; NaN and infinities become ``None``."""
        return _clean(asdict(self))


def _clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_clean(v) for v in value]
    if isinstance(value, float | np.floating):
        f = float(value)
        return f if math.isfinite(f) else None
    if isinstance(value, np.integer):
        return int(value)
    return value


# ---- inputs ---------------------------------------------------------------------------


def _label_panels(
    lake: Any, request: PanelRequest, horizons: Sequence[int]
) -> dict[int, pd.DataFrame]:
    """Forward-return panels over every bar of the window, read once."""
    raw = read_bars(
        lake, request.universe, request.interval, day_start(request.start), day_end(request.end)
    )
    actions = LakeCorporateActions(lake).load(list(request.universe))
    prepared = prepare_bars(raw, actions, request.membership)
    return {
        h: evaluate(next_open_label(h), prepared, request.universe, start=day_start(request.start))
        for h in horizons
    }


def _classification(lake: Any, tickers: Sequence[str], column: str) -> dict[str, str]:
    if not callable(getattr(lake, "sql", None)):
        return {}
    sql: Callable[..., pd.DataFrame] = lake.sql
    try:
        rows = sql(
            f"SELECT id, {column} AS value FROM instruments WHERE id = ANY(?)", [list(tickers)]
        )
    except Exception as exc:  # a lake without instruments
        _log.warning("factor.tearsheet.classification_failed", column=column, error=str(exc))
        return {}
    return {str(i): str(v) for i, v in zip(rows["id"], rows["value"], strict=True) if v is not None}


def _market_caps(lake: Any, request: PanelRequest, dates: pd.DatetimeIndex) -> pd.DataFrame | None:
    """Market cap known on each date (the latest print on or before it)."""
    if not callable(getattr(lake, "sql", None)):
        return None
    sql: Callable[..., pd.DataFrame] = lake.sql
    try:
        rows = sql(
            "SELECT ticker, date, market_cap FROM market_cap_history "
            "WHERE ticker = ANY(?) AND date BETWEEN ? AND ?",
            [
                list(request.universe),
                request.start - timedelta(days=45),
                request.end,
            ],
        )
    except Exception as exc:
        _log.warning("factor.tearsheet.market_cap_failed", error=str(exc))
        return None
    if rows.empty:
        return None
    wide = rows.pivot_table(index="date", columns="ticker", values="market_cap", aggfunc="last")
    wide.index = pd.DatetimeIndex(pd.to_datetime(wide.index))
    days = pd.DatetimeIndex(np.asarray(dates, dtype="datetime64[D]"))
    known = wide.reindex(wide.index.union(days)).sort_index().ffill(limit=None)
    out = known.reindex(days)
    out.index = dates
    return out.reindex(columns=list(request.universe))


def _dollar_volume(lake: Any, request: PanelRequest, dates: pd.DatetimeIndex) -> pd.DataFrame:
    """Trailing mean of close times volume, in raw units (a split leaves the
    product unchanged)."""
    warm = timedelta(days=_DOLLAR_VOLUME_BARS * 2 + 10)
    raw = read_bars(
        lake,
        request.universe,
        request.interval,
        day_start(request.start - warm),
        day_end(request.end),
    )
    if raw.empty:
        return pd.DataFrame(index=dates, columns=list(request.universe), dtype=float)
    raw = raw.assign(dv=raw["close"].astype(float) * raw["volume"].astype(float))
    wide = raw.pivot_table(index="timestamp", columns="ticker", values="dv", aggfunc="last")
    wide.index = pd.DatetimeIndex(pd.to_datetime(wide.index))
    trailing = cast(pd.DataFrame, wide.rolling(_DOLLAR_VOLUME_BARS, min_periods=5).mean())
    return trailing.reindex(dates).reindex(columns=list(request.universe))


def _size_panel(
    lake: Any, request: PanelRequest, dates: pd.DatetimeIndex
) -> tuple[pd.DataFrame | None, SizeBasis]:
    caps = _market_caps(lake, request, dates)
    if caps is not None and caps.notna().mean(axis=1).mean() >= 0.5:
        return caps, "market_cap"
    try:
        return _dollar_volume(lake, request, dates), "dollar_volume"
    except Exception as exc:
        _log.warning("factor.tearsheet.size_failed", error=str(exc))
        return None, "none"


# ---- statistics -----------------------------------------------------------------------


def _hac_lags(horizon: int, every_bars: int) -> int:
    return max(0, math.ceil(horizon / every_bars) - 1)


def _horizon_summary(
    horizon: int, scores: pd.DataFrame, fwd: pd.DataFrame, opts: TearSheetOptions
) -> tuple[HorizonSummary, pd.Series]:
    ic = ic_by_date(scores, fwd, min_names=opts.min_names)
    lags = _hac_lags(horizon, opts.every_bars)
    x = ic.to_numpy(float)
    mean, std, _, se_hac = _mean_se(x, lags)
    finite = x[np.isfinite(x)]
    q = _quantile_returns(
        scores.to_numpy(float), fwd.to_numpy(float), opts.n_quantiles, opts.min_names
    )
    spread = q[:, -1] - q[:, 0]
    s_mean, _, _, s_se = _mean_se(spread, lags)
    q_means = [
        float(np.nanmean(q[:, j])) if np.isfinite(q[:, j]).any() else math.nan
        for j in range(opts.n_quantiles)
    ]
    summary = HorizonSummary(
        horizon=horizon,
        n_dates=int(finite.size),
        mean_ic=mean,
        ic_std=std,
        icir=_ratio(mean, std),
        hit_rate=float((finite > 0).mean()) if finite.size else math.nan,
        t_stat_hac=_ratio(mean, se_hac),
        hac_lags=lags,
        quantile_means=q_means,
        spread_mean=s_mean,
        spread_t_hac=_ratio(s_mean, s_se),
    )
    return summary, ic


def _periods(
    factor: Factor,
    scores: pd.DataFrame,
    fwd: pd.DataFrame,
    ic: pd.Series,
    opts: TearSheetOptions,
    lags: int,
) -> list[PeriodIC]:
    """The IC and spread at the main horizon per period of the factor's
    paper (module doc)."""
    provenance = factor.provenance
    if provenance is None:
        return []
    stamps = pd.DatetimeIndex(scores.index)
    labels = np.array([provenance.period_of(d.date()) for d in stamps])
    q = _quantile_returns(
        scores.to_numpy(float), fwd.to_numpy(float), opts.n_quantiles, opts.min_names
    )
    spread = q[:, -1] - q[:, 0]
    x = ic.reindex(stamps).to_numpy(float)
    out: list[PeriodIC] = []
    for period in ("pre_sample", "in_sample", "post_sample", "post_publication"):
        mask = labels == period
        if not mask.any():
            continue
        mean, _, _, se = _mean_se(x[mask], lags)
        s_mean = _mean_se(spread[mask], lags)[0]
        days = stamps[mask]
        out.append(
            PeriodIC(
                period=period,
                start=str(days[0].date()),
                end=str(days[-1].date()),
                n_dates=int(np.isfinite(x[mask]).sum()),
                mean_ic=mean,
                t_stat_hac=_ratio(mean, se),
                spread_mean=s_mean,
            )
        )
    return out


def _group_ics(
    scores: pd.DataFrame,
    fwd: pd.DataFrame,
    labels: Callable[[int], Sequence[str | None]],
    order: Sequence[str] | None,
    lags: int,
    min_names: int,
) -> list[GroupIC]:
    """IC within each group; ``labels(t)`` names each column's group on
    row ``t`` (``None`` leaves the name out)."""
    s, f = scores.to_numpy(float), fwd.to_numpy(float)
    by_row = [list(labels(t)) for t in range(s.shape[0])]
    groups = sorted({g for row in by_row for g in row if g is not None})
    if order is not None:
        groups = [g for g in order if g in groups] + [g for g in groups if g not in order]
    out: list[GroupIC] = []
    for group in groups:
        mask = np.array([[g == group for g in row] for row in by_row], dtype=bool)
        gs = np.where(mask, s, np.nan)
        gf = np.where(mask, f, np.nan)
        ic = ic_by_date(pd.DataFrame(gs), pd.DataFrame(gf), min_names=min_names).to_numpy(float)
        mean, _, _, se = _mean_se(ic, lags)
        ok = np.isfinite(ic)
        names = (np.isfinite(gs) & np.isfinite(gf)).sum(axis=1)
        out.append(
            GroupIC(
                group=group,
                n_dates=int(ok.sum()),
                mean_names=float(names[ok].mean()) if ok.any() else 0.0,
                mean_ic=mean,
                t_stat_hac=_ratio(mean, se),
            )
        )
    return out


def _size_labels(size: pd.DataFrame, n: int) -> Callable[[int], list[str | None]]:
    names = _SIZE_LABELS.get(n) or tuple(f"size_{i + 1}" for i in range(n))
    values = size.to_numpy(float)
    rows: list[list[str | None]] = []
    for t in range(values.shape[0]):
        row: list[str | None] = [None] * values.shape[1]
        ok = np.nonzero(np.isfinite(values[t]))[0]
        if ok.size >= n:
            order = np.argsort(values[t, ok], kind="stable")
            ranks = np.empty(ok.size, dtype=int)
            ranks[order] = np.arange(ok.size)
            for j, r in zip(ok, (ranks * n) // ok.size, strict=True):
                row[int(j)] = names[int(r)]
        rows.append(row)
    return lambda t: rows[t]


def _curves_and_alpha(
    scores: pd.DataFrame,
    period: pd.DataFrame,
    opts: TearSheetOptions,
    direction: int,
    bars_per_year: float,
) -> tuple[QuantileCurves, AlphaBeta]:
    s, f = scores.to_numpy(float), period.to_numpy(float)
    q = _quantile_returns(s, f, opts.n_quantiles, opts.min_names)
    ok = np.isfinite(q).all(axis=1)
    if not ok.any():
        return QuantileCurves(), AlphaBeta()
    dates = [str(pd.Timestamp(d).date()) for d in scores.index[ok]]
    qr = q[ok]
    curves = np.cumprod(1.0 + qr, axis=0) - 1.0
    spread = np.cumprod(1.0 + (qr[:, -1] - qr[:, 0])) - 1.0
    qc = QuantileCurves(
        dates=dates,
        series=[curves[:, j].tolist() for j in range(opts.n_quantiles)],
        spread=spread.tolist(),
    )
    # rows in ``ok`` hold enough names with a value and a return
    market = np.nanmean(np.where(np.isfinite(s), f, np.nan)[ok], axis=1)
    book = direction * (qr[:, -1] - qr[:, 0])
    return qc, _regress(book, market, bars_per_year / opts.every_bars)


def _regress(y: np.ndarray, x: np.ndarray, periods_per_year: float) -> AlphaBeta:
    ok = np.isfinite(y) & np.isfinite(x)
    y, x = y[ok], x[ok]
    n = int(y.size)
    if n < 3 or float(np.var(x)) == 0.0:
        return AlphaBeta(n_periods=n)
    design = np.column_stack([np.ones(n), x])
    coef, *_ = np.linalg.lstsq(design, y, rcond=None)
    resid = y - design @ coef
    dof = n - 2
    sigma2 = float(resid @ resid) / dof
    cov = sigma2 * np.linalg.inv(design.T @ design)
    total = float(((y - y.mean()) ** 2).sum())
    return AlphaBeta(
        n_periods=n,
        alpha_annual=float(coef[0]) * periods_per_year,
        alpha_t=_ratio(float(coef[0]), math.sqrt(max(float(cov[0, 0]), 0.0))),
        beta=float(coef[1]),
        r_squared=1.0 - float(resid @ resid) / total if total > 0 else math.nan,
    )


def _dated(series: pd.Series) -> list[tuple[str, float]]:
    stamps = pd.DatetimeIndex(series.index)
    return [
        (str(stamp.date()), float(v))
        for stamp, v in zip(stamps, series.to_numpy(float), strict=True)
    ]


def _bars_per_year(lake: Any, request: PanelRequest) -> float:
    """Bars a year for the universe's most common asset class."""
    classes = list(_classification(lake, request.universe, "asset_class").values())
    common = max(set(classes), key=classes.count) if classes else "equity"
    asset_class = common if common in get_args(AssetClass) else "equity"
    return periods_per_year(cast(AssetClass, asset_class), request.interval)


def _monthly(ic: pd.Series) -> list[MonthlyIC]:
    clean = ic.dropna()
    if clean.empty:
        return []
    sums: dict[tuple[int, int], list[float]] = {}
    for stamp, value in zip(pd.DatetimeIndex(clean.index), clean.to_numpy(float), strict=True):
        sums.setdefault((stamp.year, stamp.month), []).append(float(value))
    out = []
    for year in sorted({y for y, _ in sums}):
        months: list[float | None] = [
            float(np.mean(sums[(year, m)])) if (year, m) in sums else None for m in range(1, 13)
        ]
        out.append(MonthlyIC(year=year, months=months))
    return out


# ---- entry point ----------------------------------------------------------------------


def factor_tearsheet(
    factor: Factor,
    lake: Any,
    request: PanelRequest,
    options: TearSheetOptions | None = None,
    *,
    engine: FactorEngine | None = None,
) -> FactorTearSheet:
    """The tear sheet of ``factor`` over ``request`` (module doc). ``engine``
    brings a panel cache; without one the panel is computed directly."""
    opts = options or TearSheetOptions()
    engine = engine or FactorEngine(lake)
    base = {
        "factor": factor.to_dict(),
        "window": (request.start.isoformat(), request.end.isoformat()),
        "interval": request.interval.code,
        "universe_id": request.universe_id,
        "n_tickers": len(request.universe),
        "every_bars": opts.every_bars,
        "n_quantiles": opts.n_quantiles,
    }
    if len(request.universe) < MIN_UNIVERSE:
        return FactorTearSheet(
            **base,
            n_dates=0,
            status="n/a",
            note=f"needs at least {MIN_UNIVERSE} tickers, got {len(request.universe)}",
        )
    labels = _label_panels(lake, request, sorted({*opts.horizons, opts.every_bars}))
    timeline = labels[opts.every_bars].index
    sampled = pd.DatetimeIndex(timeline[:: opts.every_bars])
    if sampled.empty:
        return FactorTearSheet(**base, n_dates=0, status="n/a", note="no bars in the window")
    scores = engine.panel(factor, request, dates=sampled).reindex(
        index=sampled, columns=list(request.universe)
    )
    coverage = float(scores.notna().mean(axis=1).mean())

    summaries: list[HorizonSummary] = []
    series: dict[int, pd.Series] = {}
    for h in opts.horizons:
        summary, ic = _horizon_summary(h, scores, labels[h].reindex(sampled), opts)
        summaries.append(summary)
        series[h] = ic
    main = 21 if 21 in opts.horizons else opts.horizons[-1]
    main_fwd = labels[main].reindex(sampled)
    lags = _hac_lags(main, opts.every_bars)

    by_group: dict[str, list[GroupIC]] = {}
    for column in ("sector", "asset_class"):
        classes = _classification(lake, request.universe, column)
        row = [classes.get(t, "unknown") for t in request.universe]
        by_group[column] = _group_ics(
            scores, main_fwd, lambda _t, r=row: r, None, lags, opts.min_names
        )
    size, basis = _size_panel(lake, request, sampled)
    if size is not None:
        names = _SIZE_LABELS.get(opts.size_buckets)
        by_group["size"] = _group_ics(
            scores,
            main_fwd,
            _size_labels(size, opts.size_buckets),
            list(names) if names else None,
            lags,
            max(2, min(opts.min_names, len(request.universe) // opts.size_buckets)),
        )

    bars_year = _bars_per_year(lake, request)
    curves, alpha = _curves_and_alpha(
        scores, labels[opts.every_bars].reindex(sampled), opts, factor.direction, bars_year
    )
    score_turnover, top_turnover = _turnover(
        scores.to_numpy(float), opts.n_quantiles, opts.min_names
    )
    main_ic = series[main]
    return FactorTearSheet(
        **base,
        n_dates=len(sampled),
        status="ok",
        coverage=coverage,
        horizons=summaries,
        ic_horizon=main,
        ic_series=_dated(main_ic.dropna()),
        ic_by_group=by_group,
        size_basis=basis,
        quantile_curves=curves,
        alpha_beta=alpha,
        monthly_ic=_monthly(main_ic),
        score_turnover=score_turnover,
        top_quantile_turnover=top_turnover,
        periods=_periods(factor, scores, main_fwd, main_ic, opts, lags),
    )

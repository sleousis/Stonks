"""Style exposures and style factor returns from the factor library
(roadmap 22.4).

Each style is one library factor:

| Style | Factor |
|-------|--------|
| momentum | ``mom_12_1`` |
| size | ``size_dv_60`` (log dollar volume) |
| value | ``book_to_market`` (equities only) |
| volatility | ``low_vol_60`` |

plus the ``sector`` of each instrument. Values are raw (the risk model
standardises them, :func:`stonks.portfolio.factor_model.standardize_exposures`).

- :func:`style_exposures`: one decision. Each factor is read with
  ``values_at``, so through a point-in-time view in a backtest a value on
  ``as_of`` uses only data known at it (P12).
- :func:`style_factor_returns`: a window, for P&L attribution. The return
  of bar ``t`` is regressed on the exposures of bar ``t - 1``, so a factor
  return never uses an exposure that was not known before the move. The
  sector of each name is the label known on ``t - 1``
  (:func:`sector_labels`), and with a stored universe's membership a name
  counts only on bars it was a member on both days (22.10).

A style whose factor fails (no statements for value, too little history)
is left out, logged, and the rest go on.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Any

import numpy as np
import pandas as pd

from stonks.core.interval import Interval
from stonks.factors.base import ExpressionFactor
from stonks.factors.engine import PanelRequest, membership_spans
from stonks.factors.registry import get_factor
from stonks.logging import get_logger
from stonks.portfolio.factor_model import (
    SECTOR,
    STYLE_FACTORS,
    cross_section_returns,
    standardize_exposures,
)
from stonks.strategies._common import as_datetime

__all__ = [
    "STYLE_FACTOR_IDS",
    "safe_style_exposures",
    "sector_labels",
    "sectors_of",
    "style_exposures",
    "style_factor_returns",
    "uses_style_model",
]

_log = get_logger("stonks.factors.style")

#: The library factor behind each style.
STYLE_FACTOR_IDS: Mapping[str, str] = {
    "momentum": "mom_12_1",
    "size": "size_dv_60",
    "value": "book_to_market",
    "volatility": "low_vol_60",
}

_DAILY_RETURN = ExpressionFactor("daily_return", "$close/Ref($close, 1)-1")


def sectors_of(lake: Any, tickers: Sequence[str]) -> dict[str, str]:
    """``instruments.sector`` per ticker (static profile data); empty when
    the lake has none."""
    try:
        rows = lake.instrument_sectors(list(tickers))
    except Exception as exc:  # a lake without instruments
        _log.warning("factor.style.sectors_failed", error=str(exc))
        return {}
    return {
        str(i): str(s)
        for i, s in zip(rows["id"], rows["sector"], strict=True)
        if isinstance(s, str) and s
    }


def sector_labels(lake: Any, tickers: Sequence[str], days: pd.DatetimeIndex) -> pd.DataFrame:
    """Days by tickers: the ``sector`` known on each day (P12).

    A day takes the latest version in ``instrument_sector_versions`` seen
    on or before it. The first version of a name counts on earlier days
    too, since it is the oldest label there is. A name with no versions
    (or a lake without the table) keeps its static ``instruments.sector``.
    Empty cells where no label is known."""
    names = list(dict.fromkeys(tickers))
    index = pd.DatetimeIndex(pd.to_datetime(days).normalize())
    out = pd.DataFrame(index=index, columns=pd.Index(names, dtype=object), dtype=object)
    versions = pd.DataFrame()
    reader = getattr(lake, "instrument_sector_versions", None)
    if callable(reader) and names:
        try:
            versions = reader(names)
        except Exception as exc:  # a lake from before migration 022
            _log.warning("factor.style.sector_versions_failed", error=str(exc))
    seen: set[str] = set()
    if not versions.empty:
        for ticker, rows in versions.groupby("id", sort=False):
            known = pd.DatetimeIndex(pd.to_datetime(rows["known_at"])).normalize()
            labels = pd.Series(rows["sector"].to_numpy(dtype=object), index=known)
            labels = labels[~labels.index.duplicated(keep="last")].sort_index()
            at = labels.index.searchsorted(index, side="right") - 1
            values = labels.to_numpy(dtype=object)[np.maximum(at, 0)]
            out[str(ticker)] = [v if isinstance(v, str) and v else None for v in values]
            seen.add(str(ticker))
    rest = [t for t in names if t not in seen]
    if rest:
        for ticker, label in sectors_of(lake, rest).items():
            out[ticker] = label
    return out


def style_exposures(
    lake: Any,
    tickers: Sequence[str],
    as_of: date | datetime,
    *,
    interval: Interval = Interval.DAY_1,
    styles: Sequence[str] = STYLE_FACTORS,
    sectors: bool = True,
) -> pd.DataFrame:
    """Raw exposures known at a decision on ``as_of``: rows ``tickers``,
    one column per style that produced a value, and ``sector``."""
    names = list(dict.fromkeys(tickers))
    at = as_datetime(as_of)
    frame = pd.DataFrame(index=pd.Index(names, dtype=object))
    for style in styles:
        factor_id = STYLE_FACTOR_IDS.get(style)
        if factor_id is None:
            raise ValueError(f"unknown style {style!r}; choose from {sorted(STYLE_FACTOR_IDS)}")
        try:
            values = get_factor(factor_id).values_at(lake, names, at, interval)
        except Exception as exc:
            _log.warning("factor.style.failed", style=style, error=str(exc))
            continue
        if values:
            frame[style] = pd.Series(values, dtype=float).reindex(names)
    if sectors:
        known = sectors_of(lake, names)
        if known:
            frame[SECTOR] = pd.Series(known, dtype=object).reindex(names)
    return frame


def uses_style_model(construction: Any) -> bool:
    """The book's constructor sizes with the ``style`` covariance estimator,
    so the market view should carry style exposures."""
    if getattr(construction, "is_single_winner", True):
        return False
    return getattr(construction, "params", {}).get("estimator") == "style"


def safe_style_exposures(
    lake: Any, tickers: Sequence[str], as_of: date | datetime
) -> pd.DataFrame | None:
    """:func:`style_exposures`, or ``None`` (logged) when it fails, so a
    decision falls back to exposures read from returns or bars."""
    if not tickers:
        return None
    try:
        frame = style_exposures(lake, tickers, as_of)
    except Exception as exc:
        _log.warning("factor.style.exposures_failed", error=str(exc))
        return None
    return frame if not frame.empty and len(frame.columns) else None


def _month_ends(index: pd.DatetimeIndex) -> pd.DatetimeIndex:
    stamps = index.to_series()
    last = stamps.groupby(index.to_period("M")).max()
    return pd.DatetimeIndex(np.asarray(last, dtype="datetime64[ns]"))


def _members(
    membership: pd.DataFrame | None, names: Sequence[str], dates: pd.DatetimeIndex
) -> pd.DataFrame | None:
    """Dates by names, true where the name was a member (``end_date``
    exclusive); ``None`` without membership."""
    if membership is None:
        return None
    days = pd.DatetimeIndex(pd.to_datetime(dates).normalize())
    mask = pd.DataFrame(False, index=days, columns=pd.Index(list(names), dtype=object))
    for ticker, lo, hi in membership_spans(membership):
        if ticker in mask.columns:
            mask.loc[(days >= lo.normalize()) & (days < hi), ticker] = True
    return mask


def style_factor_returns(
    lake: Any,
    universe: Sequence[str],
    start: date,
    end: date,
    *,
    interval: Interval = Interval.DAY_1,
    styles: Sequence[str] = STYLE_FACTORS,
    sectors: bool = True,
    membership: pd.DataFrame | None = None,
    universe_id: str | None = None,
) -> pd.DataFrame:
    """Dates by factor returns over ``start..end``: the ``market``, each
    style and each ``sector:<name>`` (see the module doc). Per-date
    factors (value) are sampled on month ends and carried forward.

    ``membership`` (``ticker, start_date, end_date`` spans of a stored
    universe, ``universe_id``) limits each bar's cross-section to the
    names that were members then."""
    request = PanelRequest(
        universe=tuple(universe),
        start=start,
        end=end,
        interval=interval,
        universe_id=universe_id,
        membership=membership,
    )
    returns = _DAILY_RETURN.panel(lake, request)
    if returns.empty or len(returns) < 2:
        return pd.DataFrame()
    dates = pd.DatetimeIndex(returns.index)
    panels: dict[str, pd.DataFrame] = {}
    for style in styles:
        factor = get_factor(STYLE_FACTOR_IDS[style])
        try:
            if factor.whole_window:
                panel = factor.panel(lake, request)
            else:
                panel = factor.panel(lake, request, _month_ends(dates)).reindex(dates).ffill()
        except Exception as exc:
            _log.warning("factor.style.panel_failed", style=style, error=str(exc))
            continue
        panels[style] = panel.reindex(index=dates, columns=list(request.universe))
    names = list(request.universe)
    labels = sector_labels(lake, names, dates) if sectors else None
    members = _members(membership, names, dates)
    rows: dict[Any, dict[str, float]] = {}
    for i in range(1, len(dates)):
        before = dates[i - 1]
        counted = names
        if members is not None:
            both = members.iloc[i - 1] & members.iloc[i]
            counted = [t for t in names if both[t]]
            if not counted:
                continue
        raw = pd.DataFrame(
            {style: panel.loc[before, counted] for style, panel in panels.items()},
            index=counted,
        )
        if labels is not None:
            known = labels.iloc[i - 1].reindex(counted)
            if known.notna().any():
                raw[SECTOR] = known.astype(object)
        got = cross_section_returns(
            returns.loc[dates[i]].reindex(counted), standardize_exposures(raw)
        )
        if got:
            rows[dates[i]] = got
    out = pd.DataFrame.from_dict(rows, orient="index").sort_index()
    return out.replace([np.inf, -np.inf], np.nan)

"""Cross-sectional helpers: rank, winsorize, z-score, group-neutralise (BL-09).

Each function takes either a Series indexed by ticker (one date's cross
section) or a DataFrame with one row per date and one column per ticker, in
which case every row is processed on its own. NaNs are ignored in the
statistics and stay NaN in the output. Standard deviations are population
(``ddof=0``) because the cross section is the whole population at that date.
"""

from __future__ import annotations

from collections.abc import Mapping

import pandas as pd

type Frame = pd.Series | pd.DataFrame

_UNGROUPED = "__ungrouped__"


def _rowwise(x: Frame, fn) -> Frame:
    if isinstance(x, pd.DataFrame):
        return x.apply(fn, axis=1)
    return fn(x)


def cs_rank(x: Frame) -> Frame:
    """Percentile rank in ``(0, 1]``; ties share their average rank."""
    return _rowwise(x, lambda s: s.rank(pct=True, method="average"))


def _winsorize(s: pd.Series, n_std: float) -> pd.Series:
    mean, std = s.mean(), s.std(ddof=0)
    if not std > 0:
        return s
    return s.clip(lower=mean - n_std * std, upper=mean + n_std * std)


def cs_winsorize(x: Frame, n_std: float = 3.0) -> Frame:
    """Clip each value to ``mean +/- n_std * std`` of its cross section."""
    if n_std <= 0:
        raise ValueError(f"n_std must be positive, got {n_std}")
    return _rowwise(x, lambda s: _winsorize(s, n_std))


_MAX_WINSOR_PASSES = 50


def _zscore(s: pd.Series, winsor: float | None) -> pd.Series:
    for _ in range(_MAX_WINSOR_PASSES):
        std = s.std(ddof=0)
        if not std > 0:
            return (s * 0.0).where(s.notna())
        z = (s - s.mean()) / std
        if winsor is None or not (z.abs() > winsor + 1e-9).any():
            return z
        s = _winsorize(s, winsor)
    return z


def cs_zscore(x: Frame, winsor: float | None = 3.0) -> Frame:
    """Cross-sectional z-score. With ``winsor`` set, values beyond
    ``+/- winsor`` std are clipped and the cross section re-standardised,
    repeated until no z exceeds ``winsor`` (one pass isn't enough: a big
    outlier inflates the std it is judged by). The output has mean 0 and
    std 1 exactly. A constant cross section gives zeros."""
    if winsor is not None and winsor <= 0:
        raise ValueError(f"winsor must be positive or None, got {winsor}")
    return _rowwise(x, lambda s: _zscore(s, winsor))


def cs_neutralize(x: Frame, groups: Mapping[str, object] | pd.Series | None) -> Frame:
    """Subtract each group's mean (e.g. sector), so every group averages 0.
    Tickers missing from ``groups`` (or mapped to None/NaN) form one
    "ungrouped" bucket; ``groups=None`` demeans the whole cross section."""

    def neutralize(s: pd.Series) -> pd.Series:
        if groups is None:
            return s - s.mean()
        labels = pd.Series(s.index.map(lambda t: groups.get(t)), index=s.index, dtype=object)
        labels = labels.where(labels.notna(), _UNGROUPED)
        return s - s.groupby(labels).transform("mean")

    return _rowwise(x, neutralize)

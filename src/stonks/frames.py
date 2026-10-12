"""Typed pandas selections.

pandas types ``frame[...]`` as ``Series | DataFrame`` whatever the key, and
leaves some ``DatetimeIndex`` attributes untyped. These helpers do the same
selection and give it the type the key implies, so callers keep a precise
type without a cast at every call site.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from typing import cast

import numpy as np
import pandas as pd


def column(frame: pd.DataFrame, name: str) -> pd.Series:
    """``frame[name]``: one column as a Series."""
    return cast(pd.Series, frame[name])


def columns(frame: pd.DataFrame, names: Iterable[str]) -> pd.DataFrame:
    """``frame[list(names)]``: the named columns in that order. A missing
    name raises ``KeyError``."""
    return cast(pd.DataFrame, frame[list(names)])


def rows(frame: pd.DataFrame, mask: pd.Series | np.ndarray) -> pd.DataFrame:
    """``frame[mask]``: the rows where the boolean ``mask`` is true."""
    return cast(pd.DataFrame, frame[mask])


def subset(series: pd.Series, mask: pd.Series | np.ndarray) -> pd.Series:
    """``series[mask]``: the entries where the boolean ``mask`` is true."""
    return cast(pd.Series, series[mask])


def index_dates(obj: pd.Series | pd.DataFrame) -> list[date]:
    """The calendar date of each timestamp in ``obj``'s index."""
    return [ts.date() for ts in pd.DatetimeIndex(obj.index)]

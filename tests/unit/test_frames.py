"""``stonks.frames``: typed pandas selections."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from stonks.frames import column, columns, index_dates, rows, subset


@pytest.fixture
def frame() -> pd.DataFrame:
    return pd.DataFrame(
        {"a": [1.0, 2.0, 3.0], "b": ["x", "y", "z"], "c": [0, 1, 0]},
        index=pd.to_datetime(["2025-01-01", "2025-01-02", "2025-01-03"]),
    )


def test_column_is_the_series(frame: pd.DataFrame) -> None:
    out = column(frame, "a")
    assert isinstance(out, pd.Series)
    pd.testing.assert_series_equal(out, frame["a"])


def test_columns_keeps_order(frame: pd.DataFrame) -> None:
    out = columns(frame, ("c", "a"))
    assert isinstance(out, pd.DataFrame)
    assert list(out.columns) == ["c", "a"]
    with pytest.raises(KeyError):
        columns(frame, ["missing"])


def test_rows_filters_by_mask(frame: pd.DataFrame) -> None:
    out = rows(frame, frame["c"] == 0)
    assert isinstance(out, pd.DataFrame)
    assert list(out["b"]) == ["x", "z"]


def test_index_dates(frame: pd.DataFrame) -> None:
    assert index_dates(frame["a"]) == [date(2025, 1, 1), date(2025, 1, 2), date(2025, 1, 3)]
    assert index_dates(frame) == [date(2025, 1, 1), date(2025, 1, 2), date(2025, 1, 3)]


def test_subset_filters_a_series(frame: pd.DataFrame) -> None:
    s = frame["a"]
    out = subset(s, s > 1.5)
    assert isinstance(out, pd.Series)
    assert list(out) == [2.0, 3.0]

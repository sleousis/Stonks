"""``log_prices``: the natural log of a price series or frame, same shape."""

from __future__ import annotations

import numpy as np
import pandas as pd

from stonks.features.library import log_prices


def test_log_prices_of_series_keeps_index() -> None:
    s = pd.Series([1.0, np.e, np.e * np.e], index=pd.date_range("2025-01-01", periods=3))
    out = log_prices(s)
    assert isinstance(out, pd.Series)
    assert out.index.equals(s.index)
    assert np.allclose(out.to_numpy(), [0.0, 1.0, 2.0])
    assert np.allclose(out.diff().dropna().to_numpy(), [1.0, 1.0])


def test_log_prices_of_frame_keeps_columns() -> None:
    df = pd.DataFrame({"A": [1.0, np.e], "B": [np.e, 1.0]})
    out = log_prices(df)
    assert isinstance(out, pd.DataFrame)
    assert list(out.columns) == ["A", "B"]
    assert np.allclose(out.to_numpy(), [[0.0, 1.0], [1.0, 0.0]])

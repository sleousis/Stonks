"""Style exposures and style factor returns from the factor library
(roadmap 22.4), read point in time."""

from __future__ import annotations

from datetime import date, datetime

import numpy as np
import pandas as pd
import pytest

from stonks.factors.registry import get_factor
from stonks.factors.style import STYLE_FACTOR_IDS, style_exposures, style_factor_returns
from stonks.store.lake import DuckDBLake
from stonks.store.pit import PointInTimeLake

DATES = pd.bdate_range("2023-01-02", "2024-06-28")
TICKERS = [f"S{i:02d}.US" for i in range(12)]
AS_OF = date(2024, 3, 28)


def _lake(path, *, shock_after: date | None = None) -> DuckDBLake:
    rng = np.random.default_rng(11)
    frames = []
    for i, ticker in enumerate(TICKERS):
        drift = 0.002 * (i - 6) / 6
        closes = 40.0 * np.exp(np.cumsum(rng.normal(drift, 0.01 + 0.002 * i, len(DATES))))
        if shock_after is not None:
            late = np.array([d.date() > shock_after for d in DATES])
            closes = np.where(late, closes * (3.0 if i % 2 else 0.2), closes)
        frames.append(
            pd.DataFrame(
                {
                    "ticker": ticker,
                    "date": [d.date() for d in DATES],
                    "open": closes,
                    "high": closes * 1.01,
                    "low": closes * 0.99,
                    "close": closes,
                    "adj_close": closes,
                    "volume": 1_000.0 * 5.0**i,
                }
            )
        )
    db = DuckDBLake(path)
    db.migrate()
    db.upsert_prices(pd.concat(frames, ignore_index=True))
    for i, ticker in enumerate(TICKERS):
        db.con.execute(
            "INSERT INTO instruments (id, asset_class, sector) VALUES (?, ?, ?)",
            [ticker, "equity", "Tech" if i % 3 else "Energy"],
        )
    return db


@pytest.fixture(scope="module")
def lake(tmp_path_factory):
    db = _lake(tmp_path_factory.mktemp("style") / "lake.duckdb")
    yield db
    db.close()


@pytest.fixture(scope="module")
def shocked(tmp_path_factory):
    db = _lake(tmp_path_factory.mktemp("shock") / "lake.duckdb", shock_after=AS_OF)
    yield db
    db.close()


def test_every_style_names_a_library_factor():
    for factor_id in STYLE_FACTOR_IDS.values():
        assert get_factor(factor_id).id == factor_id
    size = get_factor("size_dv_60")
    assert size.family == "size" and size.direction == -1


def test_style_exposures_read_the_library(lake):
    got = style_exposures(lake, TICKERS, AS_OF)
    assert {"momentum", "size", "volatility", "sector"} <= set(got.columns)
    assert "value" not in got.columns  # no statements in this lake
    assert got["size"].is_monotonic_increasing  # volume grows with i
    assert got["volatility"].corr(pd.Series(range(12), index=got.index), method="spearman") > 0.8
    assert got.loc["S00.US", "sector"] == "Energy"
    expected = get_factor("mom_12_1").values_at(lake, TICKERS, datetime(2024, 3, 28))
    assert got["momentum"].to_dict() == pytest.approx(expected)


def test_style_exposures_ignore_later_bars(lake, shocked):
    base = style_exposures(lake, TICKERS, AS_OF)
    after = style_exposures(shocked, TICKERS, AS_OF)
    pd.testing.assert_frame_equal(base, after)
    view = PointInTimeLake(shocked, datetime(2024, 3, 28))
    pd.testing.assert_frame_equal(style_exposures(view, TICKERS, AS_OF), base)


def test_unknown_style_is_refused(lake):
    with pytest.raises(ValueError, match="unknown style"):
        style_exposures(lake, TICKERS, AS_OF, styles=["beauty"])


def test_style_factor_returns_have_the_market_as_the_mean(lake):
    got = style_factor_returns(lake, TICKERS, date(2024, 2, 1), AS_OF)
    assert {"market", "momentum", "size", "volatility"} <= set(got.columns)
    assert any(c.startswith("sector:") for c in got.columns)
    day = got.index[5]
    wide = lake.sql(
        "SELECT ticker, timestamp, close FROM bars WHERE interval = '1d' AND timestamp <= ?",
        [day.to_pydatetime()],
    ).pivot(index="timestamp", columns="ticker", values="close")
    mean = float((wide.iloc[-1] / wide.iloc[-2] - 1).mean())
    # OLS with a constant: the fitted mean is the mean return. Styles are
    # standardised to mean 0; a sector dummy's mean is its share of names.
    share = {"sector:Energy": 4 / 12, "sector:Tech": 8 / 12}
    fitted = got.loc[day, "market"] + sum(
        got.loc[day, c] * share[c] for c in got.columns if c.startswith("sector:")
    )
    assert fitted == pytest.approx(mean, abs=1e-9)


def test_style_factor_returns_ignore_later_bars(lake, shocked):
    base = style_factor_returns(lake, TICKERS, date(2024, 2, 1), AS_OF)
    after = style_factor_returns(shocked, TICKERS, date(2024, 2, 1), AS_OF)
    pd.testing.assert_frame_equal(base, after)

"""Style exposures and style factor returns from the factor library
(roadmap 22.4), read point in time."""

from __future__ import annotations

from datetime import date, datetime

import numpy as np
import pandas as pd
import pytest

from stonks.factors.registry import get_factor
from stonks.factors.style import (
    STYLE_FACTOR_IDS,
    sector_labels,
    style_exposures,
    style_factor_returns,
)
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


# ---- 22.10: point-in-time sectors and membership ---------------------------------


def _relabel(db: DuckDBLake, ticker: str, sector: str, known_at: datetime) -> None:
    db.upsert_instrument_profile(
        pd.DataFrame([{"id": ticker, "asset_class": "equity", "sector": sector}]),
        known_at=known_at,
    )


@pytest.fixture
def relabeled(tmp_path):
    """The base lake, with S00 moving from Energy to Tech at a known time
    and S01 recorded as Tech from the start."""
    db = _lake(tmp_path / "lake.duckdb")
    _relabel(db, "S01.US", "Tech", datetime(2020, 1, 1))
    _relabel(db, "S00.US", "Energy", datetime(2020, 1, 1))
    _relabel(db, "S00.US", "Tech", datetime(2024, 3, 1, 15))
    yield db
    db.close()


def test_sector_labels_are_the_ones_known_on_each_day(relabeled):
    days = pd.DatetimeIndex(["2023-06-01", "2024-02-29", "2024-03-01", "2024-03-04"])
    got = sector_labels(relabeled, ["S00.US", "S01.US", "S03.US"], days)
    assert got["S00.US"].tolist() == ["Energy", "Energy", "Tech", "Tech"]
    assert got["S01.US"].tolist() == ["Tech"] * 4
    assert got["S03.US"].tolist() == ["Energy"] * 4  # no versions: the static label


def test_sector_labels_count_the_first_version_from_the_start(tmp_path):
    db = _lake(tmp_path / "lake.duckdb")
    try:
        _relabel(db, "S00.US", "Utilities", datetime(2025, 1, 1))
        got = sector_labels(db, ["S00.US"], pd.DatetimeIndex(["2023-06-01"]))
        assert got["S00.US"].tolist() == ["Utilities"]
    finally:
        db.close()


def test_a_later_reclassification_does_not_change_past_factor_returns(lake, relabeled):
    """Look-ahead: S00 is relabeled on 2024-03-01. Factor returns before the
    change match a lake that never saw it, and differ after it."""
    base = style_factor_returns(lake, TICKERS, date(2024, 2, 1), AS_OF)
    moved = style_factor_returns(relabeled, TICKERS, date(2024, 2, 1), AS_OF)
    before = base.index[base.index <= pd.Timestamp("2024-03-01")]
    pd.testing.assert_frame_equal(base.loc[before], moved.loc[before])
    after = base.index[base.index > pd.Timestamp("2024-03-04")]
    assert not np.allclose(
        base.loc[after, "sector:Energy"], moved.loc[after, "sector:Energy"], equal_nan=True
    )


def test_a_reclassification_after_the_window_changes_nothing(lake, tmp_path):
    db = _lake(tmp_path / "later.duckdb")
    try:
        _relabel(db, "S00.US", "Energy", datetime(2020, 1, 1))
        _relabel(db, "S00.US", "Tech", datetime(2024, 6, 1))
        base = style_factor_returns(lake, TICKERS, date(2024, 2, 1), AS_OF)
        pd.testing.assert_frame_equal(
            base, style_factor_returns(db, TICKERS, date(2024, 2, 1), AS_OF)
        )
    finally:
        db.close()


def test_factor_returns_count_only_members(lake):
    """S11 joins on 2024-03-01: before that the cross-section is the other
    eleven names, as if it were not in the universe."""
    join = date(2024, 3, 1)
    spans = pd.DataFrame(
        [{"ticker": t, "start_date": date(2020, 1, 1), "end_date": None} for t in TICKERS[:-1]]
        + [{"ticker": TICKERS[-1], "start_date": join, "end_date": None}]
    )
    got = style_factor_returns(lake, TICKERS, date(2024, 2, 1), AS_OF, membership=spans)
    without = style_factor_returns(lake, TICKERS[:-1], date(2024, 2, 1), AS_OF)
    everyone = style_factor_returns(lake, TICKERS, date(2024, 2, 1), AS_OF)
    early = got.index[got.index < pd.Timestamp(join)]
    pd.testing.assert_frame_equal(got.loc[early], without.loc[early])
    late = got.index[got.index > pd.Timestamp("2024-03-04")]
    pd.testing.assert_frame_equal(got.loc[late], everyone.loc[late])

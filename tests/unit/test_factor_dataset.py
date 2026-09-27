"""Factor sets and the feature dataset (roadmap 22.8): the Alpha158 set with
a next-open label, as a date-by-ticker table ready for a model."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from stonks.factors.dataset import factor_dataset
from stonks.factors.engine import PanelRequest
from stonks.factors.registry import factor_sets, resolve_factors, split_top_level
from stonks.store.lake import DuckDBLake

DATES = pd.bdate_range("2024-01-01", periods=160)
TICKERS = ["A.US", "B.US", "C.US"]


@pytest.fixture(scope="module")
def lake(tmp_path_factory):
    rng = np.random.default_rng(3)
    frames = []
    for ticker in TICKERS:
        c = 20.0 * np.exp(np.cumsum(rng.normal(0, 0.01, len(DATES))))
        frames.append(
            pd.DataFrame(
                {
                    "ticker": ticker,
                    "date": [d.date() for d in DATES],
                    "open": c * 0.995,
                    "high": c * 1.01,
                    "low": c * 0.99,
                    "close": c,
                    "adj_close": c,
                    "volume": 1e6,
                }
            )
        )
    db = DuckDBLake(tmp_path_factory.mktemp("ds") / "lake.duckdb")
    db.migrate()
    db.upsert_prices(pd.concat(frames, ignore_index=True))
    yield db
    db.close()


def test_sets_are_the_library_modules():
    sets = factor_sets()
    assert {"alpha158", "classic", "fundamentals"} <= set(sets)
    assert len(sets["alpha158"]) == 157
    assert sets["alpha158"][0] == "KMID"


def test_resolve_factors_mixes_sets_ids_and_formulas():
    got = resolve_factors("classic, KMID, Mean($close, 5)/$close, KMID")
    ids = [f.id for f in got]
    assert ids[: len(factor_sets()["classic"])] == factor_sets()["classic"]
    assert ids.count("KMID") == 1
    assert ids[-1] == "Div(Mean($close,5),$close)"
    assert split_top_level("Ref($close, 5), KMID") == ["Ref($close, 5)", "KMID"]
    with pytest.raises(ValueError, match="at least one"):
        resolve_factors(" , ")


def test_dataset_has_features_and_a_next_open_label(lake):
    request = PanelRequest(tuple(TICKERS), date(2024, 4, 1), date(2024, 8, 1))
    factors = resolve_factors("KMID,ROC5,MA20")
    data = factor_dataset(factors, lake, request, label_horizon=1)
    assert list(data.columns) == ["KMID", "ROC5", "MA20", "label"]
    assert data.index.names == ["timestamp", "ticker"]
    first = data.index.get_level_values("timestamp").min()
    assert first >= pd.Timestamp("2024-04-01")
    # MA20 is warm on the first date: the warm-up was read before the window
    assert data["MA20"].notna().all()
    # the label is O[t+2]/O[t+1]-1 and is empty on the last date of the window
    one = data.xs("A.US", level="ticker")
    raw = lake.sql("SELECT timestamp, open FROM bars WHERE ticker = 'A.US' ORDER BY timestamp")
    opens = raw.set_index(pd.to_datetime(raw["timestamp"]))["open"]
    t = one.index[10]
    pos = opens.index.get_loc(t)
    assert one.loc[t, "label"] == pytest.approx(opens.iloc[pos + 2] / opens.iloc[pos + 1] - 1)
    assert np.isnan(one["label"].iloc[-1])


def test_dataset_without_label_and_on_sampled_dates(lake):
    request = PanelRequest(tuple(TICKERS), date(2024, 4, 1), date(2024, 8, 1))
    dates = pd.DatetimeIndex(DATES[70:100:5])
    data = factor_dataset(resolve_factors("KMID"), lake, request, label_horizon=None, dates=dates)
    assert list(data.columns) == ["KMID"]
    assert set(data.index.get_level_values("timestamp")) == set(dates)


def test_write_dataset_to_csv_and_parquet(lake, tmp_path):
    import duckdb

    from stonks.factors.dataset import write_dataset

    request = PanelRequest(tuple(TICKERS), date(2024, 4, 1), date(2024, 5, 1))
    data = factor_dataset(resolve_factors("KMID"), lake, request)
    write_dataset(data, tmp_path / "d.csv")
    assert pd.read_csv(tmp_path / "d.csv").columns.tolist() == [
        "timestamp",
        "ticker",
        "KMID",
        "label",
    ]
    write_dataset(data, tmp_path / "d.parquet")
    back = duckdb.sql(f"SELECT * FROM read_parquet('{tmp_path / 'd.parquet'}')").df()
    assert len(back) == len(data)
    with pytest.raises(ValueError, match="parquet"):
        write_dataset(data, tmp_path / "d.xlsx")

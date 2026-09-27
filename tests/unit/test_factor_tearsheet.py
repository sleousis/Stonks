"""Factor tear sheets (roadmap 22.3): IC per horizon, by sector, asset class
and size, returns per quantile, factor alpha and beta, a monthly IC heatmap
and turnover, for any factor."""

from __future__ import annotations

import math
from datetime import date
from typing import Any

import numpy as np
import pandas as pd
import pytest

from stonks.factors.base import ExpressionFactor, Factor
from stonks.factors.engine import PanelRequest, panel_from_lake
from stonks.factors.expression import next_open_label
from stonks.factors.tearsheet import TearSheetOptions, factor_tearsheet
from stonks.reporting.factors import render_factor_page
from stonks.store.lake import DuckDBLake

DATES = pd.bdate_range("2024-01-01", "2024-12-31")
TICKERS = [f"Q{i:02d}.US" for i in range(20)]
START, END = date(2024, 1, 1), date(2024, 12, 31)


@pytest.fixture(scope="module")
def lake(tmp_path_factory):
    rng = np.random.default_rng(5)
    frames = []
    for i, ticker in enumerate(TICKERS):
        opens = 50.0 * np.exp(np.cumsum(rng.normal(0.0003, 0.02, len(DATES))))
        closes = opens * np.exp(rng.normal(0, 0.005, len(DATES)))
        frames.append(
            pd.DataFrame(
                {
                    "ticker": ticker,
                    "date": [d.date() for d in DATES],
                    "open": opens,
                    "high": np.maximum(opens, closes) * 1.01,
                    "low": np.minimum(opens, closes) * 0.99,
                    "close": closes,
                    "adj_close": closes,
                    "volume": 1_000_000.0 * (i + 1),
                }
            )
        )
    db = DuckDBLake(tmp_path_factory.mktemp("ts") / "lake.duckdb")
    db.migrate()
    db.upsert_prices(pd.concat(frames, ignore_index=True))
    for i, ticker in enumerate(TICKERS):
        db.con.execute(
            "INSERT INTO instruments (id, asset_class, sector) VALUES (?, ?, ?)",
            [ticker, "equity" if i < 15 else "crypto", "Tech" if i % 2 else "Energy"],
        )
    yield db
    db.close()


class Oracle(Factor):
    """Knows the next ``h``-bar open-to-open return: IC 1 at horizon ``h``.
    A test double only; real factors cannot read the future."""

    kind = "expression"
    family = "test"
    description = "the future"

    def __init__(self, horizon: int, sign: int = 1) -> None:
        self.id = f"oracle{horizon}"
        self.horizon = horizon
        self.sign = sign

    def panel(self, lake: Any, request: PanelRequest, dates=None) -> pd.DataFrame:
        out = self.sign * panel_from_lake(next_open_label(self.horizon), lake, request)
        return out if dates is None else out.reindex(dates)

    def values_at(self, lake, tickers, as_of, interval=None):  # pragma: no cover
        raise NotImplementedError


def _request(tickers=TICKERS) -> PanelRequest:
    return PanelRequest(tuple(tickers), START, END)


def test_an_oracle_has_perfect_ic_and_ordered_quantiles(lake):
    sheet = factor_tearsheet(
        Oracle(5), lake, _request(), TearSheetOptions(horizons=(1, 5), every_bars=5)
    )
    assert sheet.status == "ok"
    by_h = {h.horizon: h for h in sheet.horizons}
    assert by_h[5].mean_ic == pytest.approx(1.0)
    assert by_h[5].hit_rate == pytest.approx(1.0)
    means = by_h[5].quantile_means
    assert len(means) == 5
    assert all(a < b for a, b in zip(means, means[1:], strict=False))
    assert by_h[5].spread_mean > 0
    assert sheet.ic_horizon == 5
    # alpha of the long-short book over the equal-weight universe
    assert sheet.alpha_beta.alpha_annual > 0
    assert sheet.alpha_beta.n_periods > 10
    # cumulative returns per quantile: the top bucket ends highest
    ends = [series[-1] for series in sheet.quantile_curves.series]
    assert ends[-1] == max(ends)
    assert len(sheet.quantile_curves.dates) == len(sheet.quantile_curves.series[0])


def test_ic_by_sector_asset_class_and_size(lake):
    sheet = factor_tearsheet(
        Oracle(5), lake, _request(), TearSheetOptions(horizons=(5,), every_bars=5, min_names=3)
    )
    sectors = {g.group: g for g in sheet.ic_by_group["sector"]}
    assert set(sectors) == {"Tech", "Energy"}
    assert sectors["Tech"].mean_ic == pytest.approx(1.0)
    classes = {g.group for g in sheet.ic_by_group["asset_class"]}
    assert classes == {"equity", "crypto"}
    sizes = [g.group for g in sheet.ic_by_group["size"]]
    assert sizes == ["small", "mid", "large"]
    assert sheet.size_basis == "dollar_volume"


def test_size_uses_market_cap_when_the_lake_has_it(lake, tmp_path):
    rows = [
        {"ticker": t, "date": d.date(), "market_cap": 1e9 * (20 - i)}
        for i, t in enumerate(TICKERS)
        for d in DATES[::5]
    ]
    lake.upsert_market_cap_history(pd.DataFrame(rows))
    try:
        sheet = factor_tearsheet(
            Oracle(5), lake, _request(), TearSheetOptions(horizons=(5,), every_bars=5)
        )
        assert sheet.size_basis == "market_cap"
    finally:
        lake.con.execute("DELETE FROM market_cap_history")


def test_monthly_heatmap_and_turnover(lake):
    sheet = factor_tearsheet(
        Oracle(5), lake, _request(), TearSheetOptions(horizons=(5,), every_bars=5)
    )
    assert [row.year for row in sheet.monthly_ic] == [2024]
    months = sheet.monthly_ic[0].months
    assert len(months) == 12
    assert months[0] == pytest.approx(1.0)
    assert months[11] is None or months[11] == pytest.approx(1.0)
    assert 0 <= sheet.top_quantile_turnover <= 1
    assert len(sheet.ic_series) == sheet.horizons[0].n_dates


def test_labels_never_read_past_the_window_end(lake):
    """The last dates have no forward return inside the window, so they
    carry no IC (P12)."""
    sheet = factor_tearsheet(
        Oracle(21), lake, _request(), TearSheetOptions(horizons=(21,), every_bars=1)
    )
    last = pd.Timestamp(sheet.ic_series[-1][0])
    assert last <= pd.Timestamp(DATES[-23])


def test_a_real_factor_and_its_direction(lake):
    factor = ExpressionFactor("rev5", "Ref($close, 5)/$close-1", family="reversal")
    sheet = factor_tearsheet(factor, lake, _request(), TearSheetOptions(horizons=(1, 5)))
    assert sheet.status == "ok"
    assert all(math.isfinite(h.mean_ic) for h in sheet.horizons)
    assert sheet.factor["id"] == "rev5"
    assert 0 < sheet.coverage <= 1
    flipped = factor_tearsheet(
        Oracle(5, sign=-1), lake, _request(), TearSheetOptions(horizons=(5,), every_bars=5)
    )
    assert flipped.horizons[0].mean_ic == pytest.approx(-1.0)


def test_small_universe_is_not_applicable(lake):
    sheet = factor_tearsheet(Oracle(5), lake, _request(TICKERS[:4]), TearSheetOptions())
    assert sheet.status == "n/a"
    assert "at least" in sheet.note
    assert sheet.horizons == []


def test_options_are_checked():
    with pytest.raises(ValueError, match="horizon"):
        TearSheetOptions(horizons=(0,))
    with pytest.raises(ValueError, match="every_bars"):
        TearSheetOptions(every_bars=0)
    with pytest.raises(ValueError, match="quantiles"):
        TearSheetOptions(n_quantiles=1)


def test_to_dict_is_json_safe_and_renders(lake):
    import json

    sheet = factor_tearsheet(
        Oracle(5), lake, _request(), TearSheetOptions(horizons=(1, 5), every_bars=5)
    )
    payload = sheet.to_dict()
    text = json.dumps(payload, allow_nan=False)
    assert "monthly_ic" in text
    page = render_factor_page(sheet)
    assert page.startswith("<!doctype html>")
    assert "oracle5" in page
    assert "Monthly IC" in page
    small = factor_tearsheet(Oracle(5), lake, _request(TICKERS[:4]), TearSheetOptions())
    assert "n/a" in render_factor_page(small)

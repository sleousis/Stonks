"""Factor provenance (roadmap 23.13): each published factor names its paper,
sample years and reported statistic, and tear sheets split the IC into
in-sample, post-sample and post-publication periods (McLean and Pontiff)."""

from __future__ import annotations

from datetime import date

import pytest

from stonks.factors.base import ExpressionFactor, Provenance
from stonks.factors.registry import factor_catalog, factor_sets, get_factor
from stonks.factors.tearsheet import TearSheetOptions, factor_tearsheet
from tests.unit.test_factor_tearsheet import TICKERS, Oracle, _request
from tests.unit.test_factor_tearsheet import lake as lake


def test_provenance_checks_its_years():
    with pytest.raises(ValueError, match="sample"):
        Provenance("X (2000)", published=2000, sample_start=1990, sample_end=1980, reported="r")
    with pytest.raises(ValueError, match="published"):
        Provenance("X (2000)", published=1985, sample_start=1970, sample_end=1990, reported="r")
    with pytest.raises(ValueError, match="paper"):
        Provenance("", published=2000, sample_start=1970, sample_end=1990, reported="r")


def test_provenance_labels_each_day():
    p = Provenance("X (2001)", published=2001, sample_start=1980, sample_end=1995, reported="r")
    assert p.period_of(date(1979, 12, 31)) == "pre_sample"
    assert p.period_of(date(1980, 1, 1)) == "in_sample"
    assert p.period_of(date(1995, 12, 31)) == "in_sample"
    assert p.period_of(date(1996, 1, 1)) == "post_sample"
    assert p.period_of(date(2000, 12, 31)) == "post_sample"
    assert p.period_of(date(2001, 1, 1)) == "post_publication"


def test_factor_to_dict_carries_provenance():
    p = Provenance("X (2001)", published=2001, sample_start=1980, sample_end=1995, reported="r")
    f = ExpressionFactor("x", "$close/Ref($close, 5)-1", provenance=p)
    assert f.to_dict()["provenance"] == {
        "paper": "X (2001)",
        "published": 2001,
        "sample_start": 1980,
        "sample_end": 1995,
        "reported": "r",
        "t_stat": None,
    }
    assert ExpressionFactor("y", "$close/Ref($close, 5)-1").to_dict()["provenance"] is None


def test_the_published_set_names_its_sources():
    published = factor_sets()["published"]
    assert len(published) >= 6
    for fid in published:
        f = get_factor(fid)
        assert f.provenance is not None, fid
        assert f.hypothesis and f.description
    # classic and fundamentals factors that come from a paper say so too
    for fid in ("mom_12_1", "reversal_1m", "book_to_market", "accruals", "piotroski_f"):
        assert get_factor(fid).provenance is not None, fid
    n = sum(1 for f in factor_catalog().values() if f.provenance is not None)
    assert n >= 12


def test_published_expressions_compute(lake):
    for fid in ("max_return_21", "seasonality_12m", "amihud_illiquidity"):
        f = get_factor(fid)
        out = f.panel(lake, _request())
        assert out.shape[1] == len(TICKERS)
        assert out.iloc[-1].notna().sum() > 0, fid


def test_tearsheet_splits_ic_by_publication(lake):
    oracle = Oracle(5)
    oracle.provenance = Provenance(
        "Oracle (2024)", published=2025, sample_start=2020, sample_end=2023, reported="IC 1"
    )
    sheet = factor_tearsheet(oracle, lake, _request(), TearSheetOptions(horizons=(1, 5)))
    by = {p.period: p for p in sheet.periods}
    # 2024 lies after the sample and before publication
    assert set(by) == {"post_sample"}
    assert by["post_sample"].n_dates > 10
    assert by["post_sample"].mean_ic == pytest.approx(1.0)
    assert sheet.to_dict()["periods"][0]["period"] == "post_sample"


def test_tearsheet_without_provenance_has_no_periods(lake):
    sheet = factor_tearsheet(Oracle(5), lake, _request(), TearSheetOptions(horizons=(5,)))
    assert sheet.periods == []


def test_tearsheet_splits_across_the_publication_year(lake):
    oracle = Oracle(5, sign=-1)
    oracle.provenance = Provenance(
        "Oracle (2024)", published=2024, sample_start=2010, sample_end=2020, reported="r"
    )
    sheet = factor_tearsheet(oracle, lake, _request(), TearSheetOptions(horizons=(5,)))
    (only,) = sheet.periods
    assert only.period == "post_publication"
    assert only.mean_ic == pytest.approx(-1.0)
    assert only.start <= only.end


def test_the_html_tearsheet_shows_the_source_and_periods(lake):
    from stonks.reporting.factors import render_factor_page

    oracle = Oracle(5)
    oracle.provenance = Provenance(
        "Oracle & Co (2024)", published=2024, sample_start=2010, sample_end=2020, reported="r"
    )
    page = render_factor_page(factor_tearsheet(oracle, lake, _request()))
    assert "Source and decay" in page
    assert "Oracle &amp; Co (2024)" in page
    assert "after publication" in page


def test_the_service_view_carries_provenance_and_periods():
    from stonks.app.factors import FactorTearSheetView, factor_view

    view = factor_view(get_factor("gross_profitability"), "published")
    assert view.provenance is not None
    assert view.provenance.t_stat == 2.49
    period = {
        "period": "post_publication",
        "start": "2024-01-02",
        "end": "2024-12-30",
        "n_dates": 40,
        "mean_ic": 0.01,
        "t_stat_hac": None,
        "spread_mean": 0.002,
    }
    sheet = FactorTearSheetView.model_validate(
        {
            "factor": view.model_dump(),
            "window": ("2024-01-01", "2024-12-31"),
            "interval": "1d",
            "n_tickers": 20,
            "n_dates": 3,
            "every_bars": 5,
            "n_quantiles": 5,
            "status": "ok",
            "periods": [period],
        }
    )
    assert sheet.periods[0].period == "post_publication"

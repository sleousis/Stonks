"""Statement factors of the published set (roadmap 23.13): asset growth,
gross profitability and net share issuance, read point in time."""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta

import pandas as pd
import pytest

from stonks.factors.registry import get_factor
from stonks.store.lake import DuckDBLake


def _row(ticker: str, pe: date, filed: date, **values: float) -> dict:
    return {"ticker": ticker, "period_end": pe, "frequency": "A", "filing_date": filed, **values}


@pytest.fixture
def lake(tmp_path):
    db = DuckDBLake(tmp_path / "lake.duckdb")
    db.migrate()
    y1, y2 = date(2022, 12, 31), date(2023, 12, 31)
    f1, f2 = date(2023, 2, 20), date(2024, 2, 20)
    db.upsert_balance_sheet(
        pd.DataFrame(
            [
                _row("A.US", y1, f1, total_assets=100.0, common_stock_shares_outstanding=10.0),
                _row("A.US", y2, f2, total_assets=150.0, common_stock_shares_outstanding=11.0),
                _row("S.US", y1, f1, total_assets=100.0, common_stock_shares_outstanding=10.0),
                _row("S.US", y2, f2, total_assets=90.0, common_stock_shares_outstanding=20.0),
            ]
        )
    )
    db.upsert_income_statement(
        pd.DataFrame(
            [
                _row("A.US", y2, f2, gross_profit=45.0),
                _row("S.US", y2, f2, revenue=100.0, cost_of_revenue=64.0),
            ]
        )
    )
    # S.US split 2 for 1 during 2023, so its share count doubled without issuance
    db.upsert_stock_splits(
        pd.DataFrame([{"ticker": "S.US", "date": date(2023, 6, 1), "ratio": 2.0}])
    )
    yield db
    db.close()


AFTER = datetime(2024, 3, 1)


def _values(lake, fid, as_of=AFTER):
    return get_factor(fid).values_at(lake, ["A.US", "S.US", "NONE.US"], as_of)


def test_asset_growth(lake):
    got = _values(lake, "asset_growth")
    assert got == pytest.approx({"A.US": 0.5, "S.US": -0.1})


def test_gross_profitability_falls_back_to_revenue_minus_cost(lake):
    got = _values(lake, "gross_profitability")
    assert got == pytest.approx({"A.US": 45.0 / 150.0, "S.US": 36.0 / 90.0})


def test_net_share_issuance_nets_out_splits(lake):
    got = _values(lake, "net_share_issuance")
    assert got["A.US"] == pytest.approx(math.log(1.1))
    assert got["S.US"] == pytest.approx(0.0)


def test_a_report_counts_from_the_day_after_its_filing(lake):
    # on the filing day of the 2023 report only 2022 is known: one year, no growth
    assert _values(lake, "asset_growth", datetime(2024, 2, 20)) == {}
    assert _values(lake, "asset_growth", datetime(2024, 2, 21))["A.US"] == pytest.approx(0.5)


def test_stale_reports_give_no_value(lake):
    late = AFTER + timedelta(days=700)
    assert _values(lake, "gross_profitability", late) == {}


def test_panel_on_sampled_dates(lake):
    from stonks.core.interval import Interval
    from stonks.factors.engine import PanelRequest

    factor = get_factor("asset_growth")
    request = PanelRequest(("A.US", "S.US"), date(2024, 1, 1), date(2024, 3, 29), Interval.DAY_1)
    dates = pd.DatetimeIndex(["2024-01-31", "2024-03-01"])
    panel = factor.panel(lake, request, dates=dates)
    assert math.isnan(panel.loc["2024-01-31", "A.US"])
    assert panel.loc["2024-03-01", "A.US"] == pytest.approx(0.5)
    assert set(factor.tables) == {"income_statement", "balance_sheet"}
    assert "stock_splits" in get_factor("net_share_issuance").tables

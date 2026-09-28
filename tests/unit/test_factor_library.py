"""The factor registry and library (roadmap 22.2, 22.8): discovery, the
Alpha158 set with hand-checked values, classic factors and the
fundamentals scores read point in time."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from stonks.factors.base import ExpressionFactor, Factor
from stonks.factors.engine import PanelRequest, evaluate, prepare_bars
from stonks.factors.expression import ExpressionError, parse_factor
from stonks.factors.library import alpha158
from stonks.factors.registry import factor_catalog, get_factor, resolve_factor
from stonks.store.lake import DuckDBLake
from stonks.store.pit import PointInTimeLake
from tests.unit.test_quant_value import FISCAL_YEARS, _normal_year, _prices, _write_years


def test_the_catalog_discovers_every_library_module():
    catalog = factor_catalog()
    kinds = {f.kind for f in catalog.values()}
    assert kinds == {"expression", "fundamental"}
    assert sum(1 for f in catalog.values() if f.id in _alpha158_ids()) == 157
    for fid in ("KMID", "ROC20", "CORR60", "mom_12_1", "low_vol_60", "piotroski_f", "ebit_tev"):
        assert fid in catalog
    assert all(isinstance(f, Factor) and f.description and f.hypothesis for f in catalog.values())
    assert all(f.direction in (1, -1) for f in catalog.values())


def _alpha158_ids() -> set[str]:
    return {f.id for f in alpha158.factors()}


def test_every_expression_in_the_library_is_point_in_time():
    for factor in factor_catalog().values():
        if isinstance(factor, ExpressionFactor):
            node = parse_factor(factor.expression)
            assert str(node) == factor.expression
            assert factor.lookback_bars >= 0


def test_get_and_resolve():
    assert get_factor("KMID").expression == "Div(Sub($close,$open),$open)"
    with pytest.raises(ValueError, match="unknown factor"):
        get_factor("nope")
    assert resolve_factor(" KMID ") is get_factor("KMID")
    adhoc = resolve_factor("Mean($close, 5) / $close")
    assert adhoc.id == "Div(Mean($close,5),$close)"
    assert adhoc.kind == "expression"
    with pytest.raises(ExpressionError):
        resolve_factor("$close > 10")
    with pytest.raises(ExpressionError):
        resolve_factor("Ref($close, -1)")


def test_expression_factor_validates_direction():
    with pytest.raises(ValueError, match="direction"):
        ExpressionFactor("x", "$close/$open", direction=0)
    f = ExpressionFactor("x", "$close/$open", family="custom")
    assert f.to_dict()["expression"] == "Div($close,$open)"
    assert "x" in repr(f)
    # renaming keeps the cache token: it is the formula
    assert f.cache_token == ExpressionFactor("y", "$close / $open").cache_token


# ---- Alpha158 values by hand -------------------------------------------------------------


def _one_ticker(rows: list[tuple[float, float, float, float, float]]) -> pd.DataFrame:
    dates = pd.bdate_range("2024-01-01", periods=len(rows))
    return pd.DataFrame(
        [
            {
                "ticker": "A",
                "timestamp": d,
                "open": o,
                "high": h,
                "low": low,
                "close": c,
                "adj_close": c,
                "volume": v,
            }
            for d, (o, h, low, c, v) in zip(dates, rows, strict=True)
        ]
    )


BARS = _one_ticker(
    [
        (10, 12, 9, 11, 100),
        (11, 13, 10, 12, 200),
        (12, 12.5, 10, 10, 100),
        (10, 11, 8, 9, 300),
        (9, 10, 8.5, 10, 300),
        (10, 14, 10, 13, 200),
    ]
)


def _value(fid: str, row: int = -1) -> float:
    factor = get_factor(fid)
    panel = evaluate(factor.node, prepare_bars(BARS), ["A"])
    return float(panel["A"].iloc[row])


def test_kbar_features():
    assert _value("KMID") == pytest.approx((13 - 10) / 10)
    assert _value("KLEN") == pytest.approx((14 - 10) / 10)
    assert _value("KUP") == pytest.approx((14 - 13) / 10)
    assert _value("KLOW") == pytest.approx((10 - 10) / 10)
    assert _value("KSFT") == pytest.approx((2 * 13 - 14 - 10) / 10)
    assert _value("KMID2", 2) == pytest.approx((10 - 12) / (12.5 - 10 + 1e-12))
    assert _value("OPEN0") == pytest.approx(10 / 13)


def test_rolling_features_at_window_five():
    closes = [11, 12, 10, 9, 10, 13]
    assert _value("ROC5") == pytest.approx(11 / 13)
    assert _value("MA5") == pytest.approx(sum(closes[1:]) / 5 / 13)
    assert _value("MAX5") == pytest.approx(14 / 13)
    assert _value("MIN5") == pytest.approx(8 / 13)
    assert _value("RSV5") == pytest.approx((13 - 8) / (14 - 8 + 1e-12))
    assert _value("IMAX5") == pytest.approx(5 / 5)  # the high is today's bar
    assert _value("IMIN5") == pytest.approx(3 / 5)  # 8 on the third bar of the window
    assert _value("CNTP5") == pytest.approx(3 / 5)  # up on 12, 10 and 13 of the last five
    assert _value("CNTN5") == pytest.approx(2 / 5)
    moves = [1, -2, -1, 1, 3]
    up = sum(m for m in moves if m > 0)
    assert _value("SUMP5") == pytest.approx(up / (sum(abs(m) for m in moves) + 1e-12))
    assert _value("VMA5") == pytest.approx((200 + 100 + 300 + 300 + 200) / 5 / 200)


# ---- fundamentals scores ------------------------------------------------------------------

AS_OF = date(2024, 6, 28)


@pytest.fixture(scope="module")
def fund_lake(tmp_path_factory):
    lake = DuckDBLake(tmp_path_factory.mktemp("ff") / "lake.duckdb")
    lake.migrate()
    for i, t in enumerate(("A.US", "B.US", "C.US")):
        _prices(lake, t)
        _write_years(lake, t, [_normal_year(10 + i, y, 0.0, 0.0) for y in range(4)])
    # D files its last annual report on 2024-07-15, after AS_OF
    _prices(lake, "D.US")
    years = [_normal_year(10, y, 0.0, 0.0) for y in range(4)]
    years[-1] = {**years[-1], "ebit": 900.0, "operating_income": 900.0}
    filed = [date(fy + 1, 3, 1) for fy in FISCAL_YEARS[:-1]] + [date(2024, 7, 15)]
    _write_years(lake, "D.US", years, filed=filed)
    yield lake
    lake.close()


def test_fundamental_factor_values_match_the_strategy_metrics(fund_lake):
    factor = get_factor("ebit_tev")
    got = factor.values_at(fund_lake, ["A.US", "B.US", "C.US"], pd.Timestamp(AS_OF))
    ebit_a = 50.0 + 5.0 * 10
    tev_a = 10.0 * 100 + 100.0 - (50.0 + 5.0 * 10)
    assert got["A.US"] == pytest.approx(ebit_a / tev_a)
    assert got["C.US"] > got["B.US"] > got["A.US"]
    assert factor.asset_classes == ("equity",)
    assert "income_statement" in factor.tables


def test_fundamental_panel_is_point_in_time(fund_lake):
    factor = get_factor("ebit_tev")
    dates = pd.DatetimeIndex(["2024-07-15", "2024-07-16"])
    request = PanelRequest(("D.US",), date(2024, 7, 1), date(2024, 7, 31))
    panel = factor.panel(fund_lake, request, dates)
    # the report filed on 2024-07-15 is usable from the next day; before it
    # the newest visible report (FY2022) is too old to score
    assert pd.isna(panel.loc["2024-07-15", "D.US"])
    assert panel.loc["2024-07-16", "D.US"] == pytest.approx(900.0 / 1000.0)
    view = PointInTimeLake(fund_lake, pd.Timestamp("2024-07-15").to_pydatetime())
    assert factor.values_at(view, ["D.US"], pd.Timestamp("2024-07-15").to_pydatetime()) == {}
    later = PointInTimeLake(fund_lake, pd.Timestamp("2024-07-16").to_pydatetime())
    got = factor.values_at(later, ["D.US"], pd.Timestamp("2024-07-16").to_pydatetime())
    assert got["D.US"] == pytest.approx(0.9)


def test_fundamental_panel_on_every_bar_when_no_dates(fund_lake):
    factor = get_factor("piotroski_f")
    request = PanelRequest(("A.US",), date(2024, 6, 24), date(2024, 6, 28))
    panel = factor.panel(fund_lake, request)
    assert len(panel) == 5
    assert panel["A.US"].between(0, 9).all()


def test_quality_value_composite_is_a_factor(fund_lake):
    got = get_factor("quality_value").values_at(fund_lake, ["A.US"], pd.Timestamp(AS_OF))
    assert "A.US" in got

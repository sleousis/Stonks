"""QuantValue (Gray & Carlisle): sector exclusion, forensic and distress
screens, cheapest EBIT/TEV slice, then quality; plus Piotroski and magic
formula modes. Every statement is visible only from its filing date."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pandas as pd
import pytest

from stonks.backtest.engine import BacktestConfig, Backtester
from stonks.backtest.simulated_broker import SimulatedBroker
from stonks.core.types import Portfolio
from stonks.lab.catalog import strategy_catalog
from stonks.store.lake import DuckDBLake
from stonks.strategies.base import strategy_metadata
from stonks.strategies.examples.quant_value import (
    QuantValue,
    select_magic_formula,
    select_piotroski,
    select_quant_value,
)

REBALANCE = date(2024, 6, 28)  # last session of June 2024
FISCAL_YEARS = [2020, 2021, 2022, 2023]
GOOD = {"T15.US", "T17.US"}  # improving firms among the cheapest four
NORMAL = [f"T{i:02d}.US" for i in range(18)]


# --- lake builders ------------------------------------------------------------------


def _prices(lake, ticker, price=10.0, start="2023-01-02", end="2024-12-31"):
    days = pd.bdate_range(start, end)
    lake.upsert_prices(
        pd.DataFrame(
            {
                "ticker": ticker,
                "date": [d.date() for d in days],
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "adj_close": price,
                "volume": 1_000,
            }
        )
    )


def _normal_year(i: int, y: int, growth: float, margin_step: float) -> dict:
    """A clean, unlevered-ish firm: EBIT rises with ``i`` (cheaper), and its
    accruals, NOA and manipulation probability fall with ``i``."""
    f = (1 + growth) ** y
    revenue = 1000.0 * f
    ebit = (50.0 + 5.0 * i) * f
    ni = 0.7 * ebit
    cfo = ni + i + 10.0
    cash = 50.0 + 5.0 * i
    return {
        "revenue": revenue,
        "cost_of_revenue": revenue * (0.6 - margin_step * y),
        "gross_profit": revenue * (0.4 + margin_step * y),
        "selling_general_administrative": 100.0 * f,
        "ebit": ebit,
        "operating_income": ebit,
        "net_income": ni,
        "net_income_continuing": ni,
        "depreciation_amortization": 30.0 + i,
        "total_assets": 1000.0,
        "current_assets": 400.0,
        "current_liabilities": 200.0,
        "cash_and_equivalents": cash,
        "cash_and_short_term_investments": cash,
        "net_receivables": 100.0 * f,
        "property_plant_equipment_net": 300.0,
        "long_term_debt": 100.0,
        "total_liabilities": 400.0,
        "total_stockholder_equity": 600.0,
        "retained_earnings": 300.0,
        "common_stock_shares_outstanding": 100.0,
        "operating_cash_flow": cfo,
        "free_cash_flow": cfo - 20.0,
        "capital_expenditures": -20.0,
    }


_INCOME = (
    "revenue",
    "cost_of_revenue",
    "gross_profit",
    "selling_general_administrative",
    "ebit",
    "operating_income",
    "net_income",
    "net_income_continuing",
    "depreciation_amortization",
)
_CASH = ("operating_cash_flow", "free_cash_flow", "capital_expenditures")


def _write_years(lake, ticker, years, *, fiscal_years=FISCAL_YEARS, filing_lag=60, filed=None):
    """One annual statement set per fiscal year (Dec year end), filed
    ``filing_lag`` days later unless ``filed`` gives explicit dates."""
    inc, bs, cf = [], [], []
    for k, (fy, row) in enumerate(zip(fiscal_years, years, strict=True)):
        pe = date(fy, 12, 31)
        fd = filed[k] if filed else pe + timedelta(days=filing_lag)
        key = {"ticker": ticker, "period_end": pe, "frequency": "A", "filing_date": fd}
        inc.append({**key, **{c: row[c] for c in _INCOME if c in row}})
        cf.append({**key, **{c: row[c] for c in _CASH if c in row}})
        bs.append({**key, **{c: v for c, v in row.items() if c not in _INCOME + _CASH}})
    lake.upsert_income_statement(pd.DataFrame(inc))
    lake.upsert_balance_sheet(pd.DataFrame(bs))
    lake.upsert_cash_flow_statement(pd.DataFrame(cf))


def _normal(i: int) -> list[dict]:
    good = f"T{i:02d}.US" in GOOD
    growth, step = (0.05, 0.01) if good else (-0.05, -0.01)
    return [_normal_year(i, y, growth, step) for y in range(len(FISCAL_YEARS))]


def _manipulator() -> list[dict]:
    years = [_normal_year(10, y, 0.0, 0.0) for y in range(4)]
    last = dict(years[-1])
    last.update(
        revenue=1500.0,
        cost_of_revenue=900.0,
        gross_profit=600.0,
        ebit=300.0,
        operating_income=300.0,
        net_income=210.0,
        net_income_continuing=210.0,
        net_receivables=400.0,
        current_assets=700.0,
        total_assets=1300.0,
        operating_cash_flow=50.0,
        free_cash_flow=30.0,
    )
    return [*years[:-1], last]


def _distressed() -> list[dict]:
    out = []
    for y in range(4):
        row = _normal_year(12, y, 0.0, 0.0)
        row.update(
            ebit=280.0,
            operating_income=280.0,
            net_income=196.0,
            net_income_continuing=196.0,
            operating_cash_flow=216.0,
            free_cash_flow=196.0,
            depreciation_amortization=60.0,
            current_liabilities=900.0,
            total_liabilities=1900.0,
            total_stockholder_equity=-900.0,
            retained_earnings=-800.0,
        )
        out.append(row)
    return out


def _bank() -> list[dict]:
    out = []
    for y in range(4):
        row = _normal_year(17, y, 0.05, 0.01)
        row.update(ebit=290.0, operating_income=290.0, net_income=203.0)
        out.append(row)
    return out


def _new_lake(path) -> DuckDBLake:
    lake = DuckDBLake(path)
    lake.migrate()
    return lake


def _sector(lake, ticker, sector):
    lake.upsert_instrument_profile(
        pd.DataFrame([{"id": ticker, "asset_class": "equity", "sector": sector}])
    )


@pytest.fixture(scope="module")
def lake(tmp_path_factory):
    lake = _new_lake(tmp_path_factory.mktemp("qv") / "lake.duckdb")
    for i, t in enumerate(NORMAL):
        _prices(lake, t)
        _write_years(lake, t, _normal(i))
        _sector(lake, t, "Industrials")
    for t, years in (("MANIP.US", _manipulator()), ("DISTRESS.US", _distressed())):
        _prices(lake, t)
        _write_years(lake, t, years)
    _prices(lake, "BANK.US")
    _write_years(lake, "BANK.US", _bank())
    _sector(lake, "BANK.US", "Financial Services")
    yield lake
    lake.close()


QV = {"value_pct": 0.2, "n": 2}
ALL = [*NORMAL, "MANIP.US", "DISTRESS.US", "BANK.US"]


# --- catalog and metadata -------------------------------------------------------------


def test_catalogued_with_metadata():
    assert strategy_catalog()["quant_value"] is QuantValue
    meta = strategy_metadata(QuantValue)
    assert meta.alpha_family == "value"
    assert meta.label_horizon_bars == 252
    assert meta.applicable_asset_classes == ("equity",)
    assert "forensic" in meta.hypothesis


def test_unknown_mode_is_rejected():
    with pytest.raises(ValueError):
        QuantValue({"mode": "graham"})


# --- selection on a synthetic cross section ---------------------------------------------


def _m(ebit_tev, fs=5, **kw):
    base = {
        "ebit_tev": ebit_tev,
        "fs": fs,
        "years_used": 3,
        "sta": 0.0,
        "snoa": 0.5,
        "pman": 0.01,
        "altman_z": 3.0,
        "fp_roa": 0.1,
        "fp_roc": 0.2,
        "fp_cfoa": 0.3,
        "fp_margin_growth": 0.0,
        "fp_margin_stability": 10.0,
    }
    return {**base, **kw}


def _universe(n=20):
    return {f"X{i:02d}": _m(0.01 * (i + 1), sta=-0.001 * i, snoa=0.5 - 0.001 * i) for i in range(n)}


def test_select_quant_value_drops_the_worst_forensic_names_first():
    metrics = _universe()
    metrics["X19"] = _m(0.5, sta=0.3)  # cheapest, but accrual-heavy
    metrics["X18"] = _m(0.4, pman=0.9)  # likely manipulator
    metrics["X17"] = _m(0.3, altman_z=1.0)  # distressed
    metrics["X16"] = _m(0.25, snoa=0.99)  # bloated balance sheet
    got = select_quant_value(metrics, n=30, value_pct=0.1, forensic_drop_pct=0.05)
    assert got == ["X15", "X14"]  # equal quality: cheaper first


def test_select_quant_value_ranks_the_cheap_slice_by_quality():
    metrics = _universe()
    metrics["X19"]["fs"] = 1
    metrics["X18"]["fs"] = 9
    got = select_quant_value(
        metrics, n=1, value_pct=0.25, forensic_drop_pct=0.0, altman_z_min=0.0
    )  # cheapest five: X15..X19
    assert got == ["X18"]


def test_select_quant_value_needs_positive_earnings_and_quality_inputs():
    metrics = {
        "A": _m(-0.2),
        "B": _m(0.1, fs=None),
        "C": _m(0.1, years_used=None),
        "D": _m(0.05),
    }
    got = select_quant_value(metrics, n=5, value_pct=1.0, forensic_drop_pct=0.0)
    assert got == ["D"]
    assert select_quant_value({}, n=5, value_pct=0.1, forensic_drop_pct=0.05) == []


def test_select_quant_value_keeps_names_a_screen_cannot_evaluate():
    metrics = {
        "A": _m(0.1, sta=None, snoa=None, pman=None, altman_z=None),
        "B": _m(0.05),
        "C": _m(0.04, sta=0.2, snoa=0.9, pman=0.5),
    }
    got = select_quant_value(metrics, n=5, value_pct=1.0, forensic_drop_pct=0.5)
    assert set(got) == {"A", "B"}  # each screen drops the worse half of {B, C}


def test_select_piotroski_takes_high_f_from_the_top_book_to_market_quintile():
    metrics = {f"X{i:02d}": {"bm": 0.1 * (i + 1), "f_score": 8} for i in range(10)}
    metrics["X09"]["f_score"] = 6  # cheapest, weak
    metrics["X08"]["f_score"] = 7
    metrics["X07"]["f_score"] = 9  # strong but not in the top quintile
    metrics["X05"] = {"bm": -1.0, "f_score": 9}  # negative book
    assert select_piotroski(metrics, bm_pct=0.2, f_min=7, n=30) == ["X08"]
    assert select_piotroski(metrics, bm_pct=0.4, f_min=7, n=30) == ["X07", "X06", "X08"]
    assert select_piotroski(metrics, bm_pct=0.4, f_min=7, n=1) == ["X07"]


def test_select_magic_formula_sums_the_two_ranks():
    metrics = {
        "A": {"ebit_tev": 0.20, "roc": 0.10},  # ranks 1 + 3
        "B": {"ebit_tev": 0.15, "roc": 0.50},  # ranks 2 + 1
        "C": {"ebit_tev": 0.10, "roc": 0.40},  # ranks 3 + 2
        "D": {"ebit_tev": -0.1, "roc": 0.90},  # losses: excluded
        "E": {"ebit_tev": 0.30, "roc": None},
    }
    assert select_magic_formula(metrics, n=2) == ["B", "A"]


# --- the lake cross section ----------------------------------------------------------------


def test_screens_value_and_quality_on_the_lake(lake):
    got = QuantValue(QV).score_universe(ALL, REBALANCE, lake)
    assert set(got) == GOOD
    assert all(v == 1.0 for v in got.values())


def test_forensic_and_distress_screens_bite(lake):
    s = QuantValue({**QV, "n": 30})
    picks = set(s.score_universe(ALL, REBALANCE, lake))
    assert "MANIP.US" not in picks and "DISTRESS.US" not in picks
    feats = s.extract_features("DISTRESS.US", REBALANCE, lake).values
    assert feats["altman_z"] < 1.81
    manip = s.extract_features("MANIP.US", REBALANCE, lake).values
    normal = s.extract_features("T10.US", REBALANCE, lake).values
    assert manip["sta"] > normal["sta"] and manip["pman"] > normal["pman"]
    unscreened = QuantValue(
        {**QV, "n": 30, "forensic_drop_pct": 0.0, "altman_z_min": 0.0, "value_pct": 0.1}
    ).score_universe(ALL, REBALANCE, lake)
    assert {"MANIP.US", "DISTRESS.US"} == set(unscreened)


def test_excluded_sectors_are_never_picked(lake):
    s = QuantValue({"mode": "magic_formula", "n": 1})
    assert "BANK.US" not in s.score_universe(ALL, REBALANCE, lake)
    open_door = QuantValue({"mode": "magic_formula", "n": 1, "excluded_sectors": ""})
    assert open_door.score_universe(ALL, REBALANCE, lake) == {"BANK.US": 1.0}


def test_features_report_value_quality_and_years_used(lake):
    f = QuantValue({}).extract_features("T17.US", REBALANCE, lake).values
    ebit = (50.0 + 5.0 * 17) * 1.05**3
    tev = 10.0 * 100 + 100.0 - (50.0 + 5.0 * 17)
    assert f["ebit_tev"] == pytest.approx(ebit / tev)
    assert f["market_cap"] == pytest.approx(1000.0)
    assert f["years_used"] == 4
    assert f["fs"] == 7
    assert 0 <= f["f_score"] <= 9
    assert QuantValue({}).extract_features("T17.US", REBALANCE, None).values == {}


def test_estimate_return_answers_from_the_lake_universe(lake):
    s = QuantValue({**QV, "universe": ",".join(ALL)})
    got = {t: s.estimate_return(t, REBALANCE, lake) for t in reversed(ALL)}
    assert {t for t, v in got.items() if v is not None} == GOOD
    assert s.estimate_return("T17.US", REBALANCE, None) is None


def test_piotroski_and_magic_formula_modes_on_the_lake(lake):
    # Equal book-to-market everywhere, so take the whole universe: only the
    # improving firms reach F >= 7.
    pio = QuantValue({"mode": "piotroski", "bm_pct": 1.0, "f_min": 7})
    assert set(pio.score_universe(NORMAL, REBALANCE, lake)) == GOOD
    f = pio.extract_features("T15.US", REBALANCE, lake).values
    assert f["f_score"] == 7 and f["bm"] == pytest.approx(0.6)
    mf = QuantValue({"mode": "magic_formula", "n": 1}).score_universe(NORMAL, REBALANCE, lake)
    assert mf == {"T17.US": 1.0}


# --- point in time and missing data ----------------------------------------------------------


def test_a_statement_filed_after_as_of_is_invisible(tmp_path):
    lake = _new_lake(tmp_path / "pit.duckdb")
    try:
        _prices(lake, "A.US")
        years = [_normal_year(10, y, 0.0, 0.0) for y in range(4)]
        years[-1] = {**years[-1], "ebit": 900.0, "operating_income": 900.0}
        filed = [date(fy + 1, 3, 1) for fy in FISCAL_YEARS[:-1]] + [date(2024, 7, 15)]
        _write_years(lake, "A.US", years, filed=filed)
        # FY2022 must still count as fresh in mid-July 2024.
        s = QuantValue({"max_statement_age_days": 700})
        before = s.extract_features("A.US", REBALANCE, lake).values
        assert before["years_used"] == 3
        assert before["ebit_tev"] == pytest.approx(100.0 / (1000.0 + 100.0 - 100.0))
        # Filing dates have no time of day: neither mid-session nor the
        # daily decision on the filing day knows the report (BE-22).
        midday = s.extract_features("A.US", datetime(2024, 7, 15, 12, 0), lake).values
        assert midday["years_used"] == 3
        on_the_day = s.extract_features("A.US", date(2024, 7, 15), lake).values
        assert on_the_day["years_used"] == 3
        next_morning = s.extract_features("A.US", datetime(2024, 7, 16, 10, 0), lake).values
        assert next_morning["years_used"] == 4
        after = s.extract_features("A.US", date(2024, 7, 16), lake).values
        assert after["years_used"] == 4
        assert after["ebit_tev"] == pytest.approx(900.0 / 1000.0)
    finally:
        lake.close()


def test_missing_history_degrades_to_min_years(tmp_path):
    lake = _new_lake(tmp_path / "short.duckdb")
    try:
        for t in ("A.US", "B.US"):
            _prices(lake, t)
        _write_years(lake, "A.US", [_normal(17)[k] for k in (2, 3)], fiscal_years=[2022, 2023])
        _write_years(lake, "B.US", _normal(15)[1:], fiscal_years=[2021, 2022, 2023])
        s = QuantValue({"value_pct": 1.0})
        assert s.extract_features("A.US", REBALANCE, lake).values["years_used"] == 2
        assert s.score_universe(["A.US", "B.US"], REBALANCE, lake) == {"B.US": 1.0}
        loose = QuantValue({"value_pct": 1.0, "min_years": 2})
        assert set(loose.score_universe(["A.US", "B.US"], REBALANCE, lake)) == {"A.US", "B.US"}
    finally:
        lake.close()


def test_a_gap_in_annual_reports_breaks_the_history(tmp_path):
    lake = _new_lake(tmp_path / "gap.duckdb")
    try:
        _prices(lake, "A.US")
        rows = _normal(15)
        _write_years(lake, "A.US", [rows[0], rows[2], rows[3]], fiscal_years=[2019, 2022, 2023])
        f = QuantValue({}).extract_features("A.US", REBALANCE, lake).values
        assert f["years_used"] == 2
    finally:
        lake.close()


def test_stale_fundamentals_or_prices_produce_no_pick(tmp_path):
    lake = _new_lake(tmp_path / "stale.duckdb")
    try:
        _prices(lake, "OLD.US")
        _write_years(lake, "OLD.US", _normal(17)[:3], fiscal_years=[2019, 2020, 2021])
        _prices(lake, "DEAD.US", end="2024-03-01")  # delisted before the rebalance
        _write_years(lake, "DEAD.US", _normal(17))
        _prices(lake, "LIVE.US")
        _write_years(lake, "LIVE.US", _normal(15))
        s = QuantValue({"value_pct": 1.0})
        assert s.score_universe(["OLD.US", "DEAD.US", "LIVE.US"], REBALANCE, lake) == {
            "LIVE.US": 1.0
        }
        assert "market_cap" not in s.extract_features("DEAD.US", REBALANCE, lake).values
        assert s.extract_features("OLD.US", REBALANCE, lake).values.get("years_used", 0) == 0
    finally:
        lake.close()


def test_no_statements_means_no_features(tmp_path):
    lake = _new_lake(tmp_path / "empty.duckdb")
    try:
        _prices(lake, "A.US")
        s = QuantValue({})
        assert s.score_universe(["A.US"], REBALANCE, lake) == {}
        assert "ebit_tev" not in s.extract_features("A.US", REBALANCE, lake).values
    finally:
        lake.close()


# --- decide ----------------------------------------------------------------------------------


def test_decide_only_trades_on_a_rebalance_day():
    s = QuantValue({})
    picks = [(1.0, "A"), (1.0, "B")]
    prices = {"A": 10.0, "B": 20.0, "C": 5.0}
    book = Portfolio(cash=10_000.0, positions={"C": 100.0})
    assert s.decide(picks, book, prices, date(2024, 6, 27)) == []
    assert s.decide(picks, book, prices, date(2024, 12, 31)) == []
    orders = s.decide(picks, book, prices, REBALANCE)
    assert {o.ticker for o in orders if o.side == "sell"} == {"C"}
    buys = {o.ticker: o.quantity * prices[o.ticker] for o in orders if o.side == "buy"}
    assert buys == {"A": pytest.approx(5_250.0), "B": pytest.approx(5_250.0)}


def test_semi_annual_rebalance():
    s = QuantValue({"rebalance_months": "6,12"})
    orders = s.decide([(1.0, "A")], Portfolio(cash=100.0), {"A": 10.0}, date(2024, 12, 31))
    assert [o.ticker for o in orders] == ["A"]


# --- persistence and backtest ------------------------------------------------------------------


def test_save_load_round_trip(tmp_path):
    s = QuantValue({"mode": "piotroski", "f_min": 8, "universe": "A.US,B.US"})
    s.save(tmp_path / "qv")
    assert QuantValue.load(tmp_path / "qv").params == s.params


def test_small_backtest_buys_the_selection_after_the_rebalance(lake):
    broker = SimulatedBroker(Portfolio(cash=100_000.0), slippage_bps=0.0, fee_per_trade=0.0)
    config = BacktestConfig(start=date(2024, 6, 3), end=date(2024, 7, 31), universe=ALL)
    report = Backtester([QuantValue(QV)], broker, lake, config).run()
    held = {t for t, q in broker.fetch_portfolio().positions.items() if q > 0}
    assert held == GOOD
    assert len(report.equity_curve) > 30

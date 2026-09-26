"""Pure fundamental scores in features/fundamentals.py, each against a
hand-worked example."""

from __future__ import annotations

import math

import pandas as pd
import pytest

from stonks.features import fundamentals as fx

# --- enterprise value, value and return on capital -----------------------------

BS = {
    "total_assets": 1000.0,
    "current_assets": 500.0,
    "current_liabilities": 300.0,
    "cash_and_equivalents": 100.0,
    "cash_and_short_term_investments": 150.0,
    "short_term_debt": 50.0,
    "long_term_debt": 200.0,
    "noncontrolling_interest": 10.0,
    "preferred_stock_total_equity": 20.0,
    "total_stockholder_equity": 400.0,
    "property_plant_equipment_net": 300.0,
}


def test_tev_adds_debt_preferred_and_nci_and_subtracts_cash():
    # 1000 + (50 + 200) + 20 + 10 - 150
    assert fx.tev(1000.0, BS) == pytest.approx(1130.0)


def test_tev_prefers_the_total_debt_line_and_treats_missing_lines_as_zero():
    bs = {"short_long_term_debt_total": 300.0, "short_term_debt": 50.0}
    assert fx.tev(1000.0, bs) == pytest.approx(1300.0)
    assert fx.tev(1000.0, {}) == pytest.approx(1000.0)


def test_tev_is_none_when_not_positive_or_without_a_market_cap():
    assert fx.tev(100.0, {"cash_and_short_term_investments": 150.0}) is None
    assert fx.tev(None, BS) is None
    assert fx.tev(0.0, {}) is None


def test_ebit_tev_keeps_the_sign_of_ebit():
    assert fx.ebit_tev(113.0, 1130.0) == pytest.approx(0.1)
    assert fx.ebit_tev(-113.0, 1130.0) == pytest.approx(-0.1)
    assert fx.ebit_tev(None, 1130.0) is None
    assert fx.ebit_tev(113.0, None) is None
    assert fx.ebit_tev(113.0, 0.0) is None


def test_roc_is_ebit_over_net_ppe_plus_working_capital():
    # 113 / (300 + (500 - 300))
    assert fx.roc(113.0, BS) == pytest.approx(0.226)
    assert fx.roc(-50.0, BS) == pytest.approx(-0.1)


def test_roc_is_none_without_positive_capital():
    bs = {**BS, "current_liabilities": 900.0}  # 300 + (500 - 900) < 0
    assert fx.roc(113.0, bs) is None
    assert fx.roc(113.0, {"current_assets": 1.0}) is None


def test_ebit_falls_back_to_operating_income():
    assert fx.ebit({"ebit": 5.0, "operating_income": 7.0}) == 5.0
    assert fx.ebit({"operating_income": 7.0}) == 7.0
    assert fx.ebit({}) is None


# --- accruals and net operating assets ----------------------------------------------

CUR = {
    "current_assets": 500.0,
    "cash_and_equivalents": 100.0,
    "current_liabilities": 300.0,
    "short_term_debt": 50.0,
    "total_assets": 1000.0,
    "depreciation_amortization": 20.0,
}
PREV = {
    "current_assets": 400.0,
    "cash_and_equivalents": 80.0,
    "current_liabilities": 260.0,
    "short_term_debt": 40.0,
    "total_assets": 900.0,
}


def test_sta_hand_worked():
    # (dCA 100 - dCash 20 - (dCL 40 - dSTD 10) - Dep 20) / TA 1000
    assert fx.sta(CUR, PREV) == pytest.approx(0.03)


def test_sta_uses_the_cash_flow_depreciation_when_the_income_line_is_missing():
    cur = {k: v for k, v in CUR.items() if k != "depreciation_amortization"}
    assert fx.sta({**cur, "depreciation": -20.0}, PREV) == pytest.approx(0.03)


def test_sta_is_none_when_an_input_is_missing():
    assert fx.sta({**CUR, "current_assets": None}, PREV) is None
    assert fx.sta(CUR, {}) is None
    assert fx.sta({**CUR, "total_assets": 0.0}, PREV) is None


def test_snoa_hand_worked():
    # OA = 1000 - 150 = 850; OL = 1000 - 50 - 200 - 10 - 20 - 400 = 320
    assert fx.snoa(BS) == pytest.approx((850.0 - 320.0) / 1000.0)


def test_snoa_with_negative_equity():
    # Equity is financing: OL = 1000 - 50 - 200 - 10 - 20 + 100 = 820
    neg = {**BS, "total_stockholder_equity": -100.0}
    assert fx.snoa(neg) == pytest.approx((850.0 - 820.0) / 1000.0)


def test_snoa_is_none_without_assets_or_equity():
    assert fx.snoa({**BS, "total_assets": None}) is None
    assert fx.snoa({**BS, "total_stockholder_equity": None}) is None


# --- Beneish M-score ----------------------------------------------------------------

B_PREV = {
    "revenue": 1000.0,
    "cost_of_revenue": 600.0,
    "net_receivables": 100.0,
    "current_assets": 400.0,
    "property_plant_equipment_net": 300.0,
    "total_assets": 1000.0,
    "depreciation_amortization": 30.0,
    "selling_general_administrative": 100.0,
    "current_liabilities": 200.0,
    "long_term_debt": 200.0,
    "net_income_continuing": 60.0,
    "operating_cash_flow": 60.0,
}
B_CUR = {
    "revenue": 1500.0,
    "cost_of_revenue": 1000.0,
    "net_receivables": 200.0,
    "current_assets": 500.0,
    "property_plant_equipment_net": 300.0,
    "total_assets": 1000.0,
    "depreciation_amortization": 30.0,
    "selling_general_administrative": 150.0,
    "current_liabilities": 250.0,
    "long_term_debt": 250.0,
    "net_income_continuing": 100.0,
    "operating_cash_flow": 50.0,
}


def test_beneish_indices_hand_worked():
    idx = fx.beneish_indices(B_CUR, B_PREV)
    assert idx["dsri"] == pytest.approx((200 / 1500) / (100 / 1000))
    assert idx["gmi"] == pytest.approx(0.4 / (500 / 1500))
    assert idx["aqi"] == pytest.approx(0.2 / 0.3)
    assert idx["sgi"] == pytest.approx(1.5)
    assert idx["depi"] == pytest.approx(1.0)
    assert idx["sgai"] == pytest.approx(1.0)
    assert idx["lvgi"] == pytest.approx(1.25)
    assert idx["tata"] == pytest.approx(0.05)


def test_beneish_m_hand_worked():
    expected = (
        -4.84
        + 0.920 * (4 / 3)
        + 0.528 * 1.2
        + 0.404 * (2 / 3)
        + 0.892 * 1.5
        + 0.115 * 1.0
        - 0.172 * 1.0
        + 4.679 * 0.05
        - 0.327 * 1.25
    )
    assert fx.beneish_m(B_CUR, B_PREV) == pytest.approx(expected)
    assert fx.beneish_m(B_CUR, B_PREV) == pytest.approx(-1.6042, abs=1e-4)


def test_beneish_m_of_an_unchanged_firm_is_minus_2_48():
    # Every index 1 and zero accruals: the textbook neutral point.
    same = {**B_PREV, "operating_cash_flow": 60.0, "net_income_continuing": 60.0}
    assert fx.beneish_m(same, same) == pytest.approx(-2.48)


def test_beneish_missing_index_is_neutral_but_sales_are_required():
    cur = {k: v for k, v in B_CUR.items() if k != "selling_general_administrative"}
    assert fx.beneish_indices(cur, B_PREV)["sgai"] == 1.0
    assert fx.beneish_m({**B_CUR, "revenue": None}, B_PREV) is None
    assert fx.beneish_m(B_CUR, {**B_PREV, "revenue": 0.0}) is None


def test_pman_is_the_normal_cdf_of_m():
    assert fx.pman(0.0) == pytest.approx(0.5)
    assert fx.pman(-2.48) == pytest.approx(0.006569, abs=1e-6)
    assert fx.pman(None) is None


# --- Altman Z ------------------------------------------------------------------------


def test_altman_z_hand_worked():
    row = {
        "current_assets": 500.0,
        "current_liabilities": 300.0,
        "total_assets": 1000.0,
        "retained_earnings": 300.0,
        "ebit": 100.0,
        "total_liabilities": 600.0,
        "revenue": 1500.0,
    }
    # 1.2*0.2 + 1.4*0.3 + 3.3*0.1 + 0.6*(1200/600) + 1.0*1.5
    assert fx.altman_z(row, 1200.0) == pytest.approx(3.69)


def test_altman_z_with_losses_and_negative_retained_earnings():
    row = {
        "current_assets": 100.0,
        "current_liabilities": 300.0,
        "total_assets": 1000.0,
        "retained_earnings": -400.0,
        "ebit": -50.0,
        "total_liabilities": 1200.0,
        "revenue": 500.0,
    }
    z = 1.2 * -0.2 + 1.4 * -0.4 + 3.3 * -0.05 + 0.6 * (100 / 1200) + 0.5
    assert fx.altman_z(row, 100.0) == pytest.approx(z)
    assert fx.altman_z(row, 100.0) < 1.81


def test_altman_z_is_none_on_missing_or_zero_denominators():
    row = {"current_assets": 1.0, "current_liabilities": 1.0, "total_assets": 10.0}
    assert fx.altman_z(row, 100.0) is None
    full = {
        "current_assets": 1.0,
        "current_liabilities": 1.0,
        "total_assets": 10.0,
        "retained_earnings": 1.0,
        "ebit": 1.0,
        "total_liabilities": 0.0,
        "revenue": 1.0,
    }
    assert fx.altman_z(full, 100.0) is None
    assert fx.altman_z({**full, "total_liabilities": 5.0}, None) is None


# --- Piotroski F and the financial-strength score -------------------------------------

F_PREV = {
    "net_income": 50.0,
    "operating_cash_flow": 60.0,
    "free_cash_flow": 30.0,
    "total_assets": 1000.0,
    "long_term_debt": 300.0,
    "current_assets": 400.0,
    "current_liabilities": 250.0,
    "common_stock_shares_outstanding": 100.0,
    "revenue": 800.0,
    "gross_profit": 240.0,
}
F_CUR = {
    "net_income": 80.0,  # ROA 0.08 > 0.05
    "operating_cash_flow": 120.0,  # > 0 and > net income
    "free_cash_flow": 90.0,  # FCF/TA 0.09 > ROA 0.08 and > 0.03
    "total_assets": 1000.0,
    "long_term_debt": 200.0,  # leverage fell
    "current_assets": 500.0,  # current ratio 2.0 > 1.6
    "current_liabilities": 250.0,
    "common_stock_shares_outstanding": 95.0,  # net buyback
    "revenue": 1000.0,  # turnover 1.0 > 0.8
    "gross_profit": 350.0,  # margin 0.35 > 0.30
}


def test_piotroski_signals_hand_worked():
    sig = fx.piotroski_signals(F_CUR, F_PREV)
    assert sig == dict.fromkeys(
        [
            "roa",
            "cfo",
            "delta_roa",
            "accrual",
            "delta_lever",
            "delta_liquid",
            "eq_offer",
            "delta_margin",
            "delta_turn",
        ],
        1,
    )
    assert fx.piotroski_f(F_CUR, F_PREV) == 9


def test_piotroski_f_counts_a_mixed_year():
    cur = {
        **F_CUR,
        "net_income": -10.0,  # roa 0, delta_roa 0
        "operating_cash_flow": 5.0,  # cfo 1, accrual 1 (5 > -10)
        "long_term_debt": 400.0,  # delta_lever 0
        "common_stock_shares_outstanding": 120.0,  # eq_offer 0
        "gross_profit": 200.0,  # delta_margin 0 (0.2 < 0.3)
    }
    # cfo, accrual, delta_liquid, delta_turn
    assert fx.piotroski_f(cur, F_PREV) == 4


def test_piotroski_missing_inputs_earn_no_point_and_debt_free_counts():
    cur = {**F_CUR, "operating_cash_flow": None, "long_term_debt": None}
    prev = {**F_PREV, "long_term_debt": None}
    sig = fx.piotroski_signals(cur, prev)
    assert sig["cfo"] == 0 and sig["accrual"] == 0
    assert sig["delta_lever"] == 1  # debt-free both years
    assert fx.piotroski_f(F_CUR, {}) is None
    assert fx.piotroski_f({**F_CUR, "total_assets": 0.0}, F_PREV) is None


def test_fs_score_hand_worked():
    sig = fx.fs_signals(F_CUR, F_PREV)
    assert set(sig) == {
        "roa",
        "fcfta",
        "accrual",
        "lever",
        "liquid",
        "neqiss",
        "delta_roa",
        "delta_fcfta",
        "delta_margin",
        "delta_turn",
    }
    assert fx.fs_score(F_CUR, F_PREV) == 10
    # FCF 20 < NI 80 -> accrual fails; FCF/TA fell -> delta_fcfta fails;
    # unchanged share count is not a net buyback.
    cur = {**F_CUR, "free_cash_flow": 20.0, "common_stock_shares_outstanding": 100.0}
    assert fx.fs_score(cur, F_PREV) == 7


def test_fs_score_derives_fcf_from_operating_cash_flow_and_capex():
    cur = {**F_CUR, "free_cash_flow": None, "capital_expenditures": -50.0}
    assert fx.fs_signals(cur, F_PREV)["fcfta"] == 1  # 120 - 50 = 70
    assert fx.free_cash_flow(cur) == pytest.approx(70.0)


# --- franchise power ------------------------------------------------------------------


def test_geometric_mean_return():
    assert fx.geometric_mean_return([0.1, 0.1, 0.1]) == pytest.approx(0.1)
    assert fx.geometric_mean_return([0.21, 0.0]) == pytest.approx(math.sqrt(1.21) - 1)
    assert fx.geometric_mean_return([]) is None
    assert fx.geometric_mean_return([0.1, -1.0]) is None


def test_margin_growth_and_stability():
    assert fx.margin_growth([0.2, 0.22, 0.242]) == pytest.approx(0.1)
    assert fx.margin_growth([-0.1, 0.2]) is None
    assert fx.margin_growth([0.2]) is None
    assert fx.margin_stability([0.3, 0.5]) == pytest.approx(0.4 / 0.1)
    assert fx.margin_stability([0.3, 0.3, 0.3]) == fx.MAX_MARGIN_STABILITY
    assert fx.margin_stability([0.3]) is None


def _year(ni, ebit, fcf, gp, ta=1000.0, revenue=1000.0):
    return {
        "net_income": ni,
        "ebit": ebit,
        "free_cash_flow": fcf,
        "gross_profit": gp,
        "revenue": revenue,
        "total_assets": ta,
        "property_plant_equipment_net": 300.0,
        "current_assets": 400.0,
        "current_liabilities": 200.0,
    }


def test_franchise_inputs_hand_worked():
    years = [_year(100.0, 50.0, 40.0, 300.0), _year(100.0, 50.0, 60.0, 330.0)]
    got = fx.franchise_inputs(years, min_years=2)
    assert got["years_used"] == 2
    assert got["roa"] == pytest.approx(0.1)
    assert got["roc"] == pytest.approx(0.1)  # 50 / (300 + 200)
    assert got["cfoa"] == pytest.approx(0.1)  # (40 + 60) / 1000
    assert got["margin_growth"] == pytest.approx(0.1)
    assert got["margin_stability"] == pytest.approx(0.315 / 0.015)


def test_franchise_inputs_needs_min_years_of_each_series():
    years = [_year(100.0, 50.0, 40.0, 300.0)] * 2
    assert fx.franchise_inputs(years, min_years=3) is None
    sparse = [_year(100.0, None, 40.0, 300.0)] + [_year(100.0, 50.0, 40.0, 300.0)] * 2
    got = fx.franchise_inputs(sparse, min_years=3)
    assert got is not None and got["years_used"] == 3
    assert got["roc"] is None  # only two years of EBIT
    assert got["roa"] == pytest.approx(0.1)


def test_franchise_power_ranks_the_cross_section():
    frame = pd.DataFrame(
        {
            "roa": [0.20, 0.10, 0.05],
            "roc": [0.30, 0.20, 0.10],
            "cfoa": [1.0, 0.5, 0.1],
            "margin_growth": [0.0, 0.1, -0.1],
            "margin_stability": [10.0, 1.0, 5.0],
        },
        index=["A", "B", "C"],
    )
    fp = fx.franchise_power(frame)
    assert list(fp.sort_values(ascending=False).index) == ["A", "B", "C"]
    assert fp.between(0.0, 1.0).all()


def test_franchise_power_skips_missing_components():
    frame = pd.DataFrame(
        {
            "roa": [0.2, 0.1],
            "roc": [None, None],
            "cfoa": [1.0, 0.5],
            "margin_growth": [None, None],
            "margin_stability": [None, None],
        },
        index=["A", "B"],
    )
    fp = fx.franchise_power(frame)
    assert fp["A"] > fp["B"]


def test_pct_rank_is_ascending_and_ignores_nan():
    r = fx.pct_rank(pd.Series({"a": 3.0, "b": 1.0, "c": float("nan")}))
    assert r["a"] == 1.0 and r["b"] == 0.5 and math.isnan(r["c"])

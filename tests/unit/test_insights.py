"""Portfolio insights (roadmap 15.4): pure analysis over a book, its value
history and price returns. No stores involved."""

from __future__ import annotations

import math
from datetime import date, timedelta

import pytest

from stonks.insights import (
    Book,
    Holding,
    allocation,
    beta,
    concentration,
    exposure,
    judge,
    period_pnl,
    realized_risk,
    returns_risk,
    strategy_agreement,
    weighted_returns,
)


def _book() -> Book:
    return Book(
        cash=2_000.0,
        holdings=(
            Holding("AAA.US", "AAA.US", 10, 100.0, 1_000.0, "equity", "Tech", "USD"),
            Holding("BBB.US", "BBB.US", 20, 150.0, 3_000.0, "equity", "Energy", "USD"),
            Holding("BTC-USD.CC", "BTC-USD.CC", 0.1, 40_000.0, 4_000.0, "crypto", None, "USD"),
            Holding("SAP.XETRA", "SAP.XETRA", -5, 200.0, -1_000.0, "equity", "Tech", "EUR"),
            Holding("ZZZ", None, 3, None, None),
        ),
    )


# ---- book -------------------------------------------------------------------------


def test_book_value_skips_unpriced_holdings():
    book = _book()
    assert book.total_value == pytest.approx(9_000.0)
    assert [h.symbol for h in book.unpriced] == ["ZZZ"]
    assert [h.symbol for h in book.uncovered] == ["ZZZ"]


def test_holding_side():
    assert Holding("A", "A", 1, 1.0, 1.0).side == "long"
    assert Holding("A", "A", -1, 1.0, -1.0).side == "short"


# ---- allocation -------------------------------------------------------------------


def test_allocation_by_asset_class_includes_cash_and_sums_to_one():
    slices = allocation(_book(), "asset_class")
    by_key = {s.key: s for s in slices}
    assert set(by_key) == {"equity", "crypto", "cash"}
    assert by_key["equity"].value == pytest.approx(3_000.0)
    assert by_key["equity"].holdings == 3
    assert by_key["crypto"].weight == pytest.approx(4_000 / 9_000)
    assert sum(s.weight or 0 for s in slices) == pytest.approx(1.0)
    # Largest absolute value first.
    assert [s.key for s in slices] == ["crypto", "equity", "cash"]


def test_allocation_unknown_sector_and_currency():
    sectors = {s.key: s.value for s in allocation(_book(), "sector")}
    assert sectors == {"Tech": 0.0, "Energy": 3_000.0, "unknown": 4_000.0, "cash": 2_000.0}
    currencies = {s.key: s.value for s in allocation(_book(), "currency")}
    assert currencies == {"USD": 10_000.0, "EUR": -1_000.0}


def test_allocation_by_ticker_and_empty_book():
    tickers = [s.key for s in allocation(_book(), "ticker")]
    assert tickers[0] == "BTC-USD.CC" and "cash" in tickers and "ZZZ" not in tickers
    empty = allocation(Book(cash=0.0, holdings=()), "ticker")
    assert empty == []
    only_cash = allocation(Book(cash=100.0, holdings=()), "asset_class")
    assert [(s.key, s.weight) for s in only_cash] == [("cash", 1.0)]


def test_allocation_weight_is_none_when_total_is_zero():
    book = Book(cash=-100.0, holdings=(Holding("A", "A", 1, 100.0, 100.0, "equity"),))
    assert all(s.weight is None for s in allocation(book, "asset_class"))


# ---- exposure ---------------------------------------------------------------------


def test_exposure_long_short_gross_net_and_beta():
    betas = {"AAA.US": 1.0, "BBB.US": 2.0, "SAP.XETRA": 1.0}
    exp = exposure(_book(), betas=betas, benchmark="SPY.US")
    assert exp.long_value == pytest.approx(8_000.0)
    assert exp.short_value == pytest.approx(-1_000.0)
    assert exp.gross == pytest.approx(9_000 / 9_000)
    assert exp.net == pytest.approx(7_000 / 9_000)
    # Beta over the holdings that have one: (1000*1 + 3000*2 - 1000*1) / 9000.
    assert exp.beta == pytest.approx(6_000 / 9_000)
    assert exp.beta_coverage == pytest.approx(5_000 / 9_000)
    assert exp.benchmark == "SPY.US"


def test_exposure_without_betas_or_value():
    exp = exposure(Book(cash=0.0, holdings=()), betas={}, benchmark=None)
    assert exp.gross is None and exp.net is None and exp.beta is None
    assert exp.beta_coverage == 0.0


def test_beta_of_a_levered_copy_is_its_leverage():
    bench = [0.01, -0.02, 0.015, 0.0, -0.01] * 6
    assert beta([2 * r for r in bench], bench) == pytest.approx(2.0)
    assert beta(bench[:5], bench[:5]) is None  # too few observations
    assert beta([0.0] * 30, [0.0] * 30) is None  # benchmark has no variance
    with pytest.raises(ValueError):
        beta([0.1], [0.1, 0.2])


# ---- P&L over periods --------------------------------------------------------------


def test_period_pnl_uses_the_last_value_on_or_before_each_start():
    start = date(2025, 1, 1)
    points = [(start + timedelta(days=i), 100.0 + i) for i in range(400)]
    rows = {r.period: r for r in period_pnl(points)}
    end = points[-1]
    assert rows["1d"].change == pytest.approx(1.0)
    assert rows["1w"].start_day == end[0] - timedelta(days=7)
    assert rows["1m"].start_day == end[0] - timedelta(days=30)
    assert rows["ytd"].start_day == date(end[0].year - 1, 12, 31)
    assert rows["inception"].start_value == 100.0
    assert rows["inception"].change_pct == pytest.approx(end[1] / 100.0 - 1)
    assert rows["1y"].end_day == end[0]


def test_period_pnl_without_enough_history():
    points = [(date(2026, 3, 1), 100.0), (date(2026, 3, 2), 110.0)]
    rows = {r.period: r for r in period_pnl(points)}
    assert rows["1d"].change == pytest.approx(10.0)
    assert rows["1m"].change is None and rows["1m"].start_day is None
    assert rows["inception"].change == pytest.approx(10.0)
    assert period_pnl([]) == []
    zero = {r.period: r for r in period_pnl([(date(2026, 1, 2), 0.0), (date(2026, 1, 3), 5.0)])}
    assert zero["1d"].change == 5.0 and zero["1d"].change_pct is None


# ---- risk -------------------------------------------------------------------------


def test_realized_risk_from_values():
    values = [100.0, 110.0, 99.0, 104.0, 120.0, 108.0]
    risk = realized_risk(values)
    assert risk is not None
    assert risk.observations == 5
    assert risk.max_drawdown == pytest.approx(99 / 110 - 1)
    assert risk.current_drawdown == pytest.approx(108 / 120 - 1)
    assert risk.var_95 < 0 and risk.expected_shortfall_95 <= risk.var_95
    assert risk.volatility is not None and risk.volatility > 0
    assert realized_risk([100.0, 101.0]) is None


def test_returns_risk_and_weighted_returns():
    returns = {"A": [0.01, -0.01, 0.02, -0.02], "B": [0.0, 0.0, 0.0, 0.0]}
    combined = weighted_returns({"A": 0.5, "B": 0.5}, returns)
    assert combined == pytest.approx([0.005, -0.005, 0.01, -0.01])
    risk = returns_risk(combined)
    assert risk is not None and risk.observations == 4
    assert returns_risk([0.01]) is None
    assert weighted_returns({}, {}) == []
    assert weighted_returns({"A": 1.0}, {"A": []}) == []


def test_concentration():
    c = concentration(_book())
    weights = [4_000 / 9_000, 3_000 / 9_000, 1_000 / 9_000, 1_000 / 9_000]
    assert c.largest == "BTC-USD.CC"
    assert c.top_weight == pytest.approx(weights[0])
    assert c.top5_weight == pytest.approx(sum(weights))
    hhi = sum(w * w for w in weights)
    assert c.hhi == pytest.approx(hhi)
    assert c.effective_holdings == pytest.approx(1 / hhi)
    assert c.holdings == 4
    empty = concentration(Book(cash=10.0, holdings=()))
    assert empty.largest is None and empty.hhi is None and empty.holdings == 0


# ---- strategy agreement ------------------------------------------------------------


@pytest.mark.parametrize(
    ("side", "r", "stance"),
    [
        ("long", 0.02, "agree"),
        ("long", -0.01, "disagree"),
        ("long", 0.0, "no_view"),
        ("short", -0.01, "agree"),
        ("short", 0.03, "disagree"),
        ("long", None, "no_view"),
        ("long", math.nan, "no_view"),
    ],
)
def test_judge(side, r, stance):
    got, reason = judge(side, r, "AAA.US", date(2026, 3, 20))
    assert got == stance
    assert "AAA.US" in reason


class _Fixed:
    applicable_asset_classes = ("equity",)

    def __init__(self, scores):
        self.scores = scores

    def estimate_return(self, ticker, as_of, lake):
        value = self.scores[ticker]
        if isinstance(value, Exception):
            raise value
        return value


def test_strategy_agreement_per_holding():
    book = _book()
    strategies = {
        "trend": _Fixed({"AAA.US": 0.05, "BBB.US": -0.02, "SAP.XETRA": 0.01}),
        "broken": _Fixed({"AAA.US": RuntimeError("boom"), "BBB.US": None, "SAP.XETRA": -0.01}),
    }
    rows = {r.symbol: r for r in strategy_agreement(book, strategies, date(2026, 3, 20), None)}
    aaa = {o.strategy_id: o for o in rows["AAA.US"].opinions}
    assert aaa["trend"].stance == "agree" and aaa["trend"].expected_return == 0.05
    assert aaa["broken"].stance == "error" and "RuntimeError" in aaa["broken"].reason
    assert rows["AAA.US"].agree == 1 and rows["AAA.US"].disagree == 0
    assert rows["BBB.US"].disagree == 1
    # Short position: a negative estimate agrees.
    sap = {o.strategy_id: o.stance for o in rows["SAP.XETRA"].opinions}
    assert sap == {"trend": "disagree", "broken": "agree"}
    btc = rows["BTC-USD.CC"].opinions
    assert {o.stance for o in btc} == {"not_applicable"}
    assert "crypto" in btc[0].reason
    zzz = rows["ZZZ"].opinions
    assert {o.stance for o in zzz} == {"not_applicable"} and "not covered" in zzz[0].reason


def test_strategy_agreement_without_a_date_has_no_views():
    rows = strategy_agreement(_book(), {"trend": _Fixed({})}, None, None)
    aaa = next(r for r in rows if r.symbol == "AAA.US")
    assert aaa.opinions[0].stance == "no_view" and "no prices" in aaa.opinions[0].reason

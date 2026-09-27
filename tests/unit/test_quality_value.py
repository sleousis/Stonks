"""QualityValue: a point-in-time value + quality fundamentals strategy.

Every statement is visible only from its filing date (or period end + a
conservative lag when the filing date is unknown).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pandas as pd
import pytest

from stonks.core.params import tunable_only
from stonks.core.protocols import Strategy
from stonks.core.types import Portfolio
from stonks.strategies.examples.quality_value import QualityValue

QUARTERS_2023 = [date(2023, 3, 31), date(2023, 6, 30), date(2023, 9, 30), date(2023, 12, 31)]


def _prices(lake, ticker, price, start="2023-01-02", end="2024-12-31"):
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


def _quarter(
    lake,
    ticker,
    period_end,
    *,
    filing_date="auto",
    revenue=100.0,
    gross_profit=40.0,
    net_income=10.0,
    fcf=8.0,
    equity=200.0,
    assets=400.0,
    liabilities=200.0,
    shares=100.0,
    frequency="Q",
):
    """Write one statement set per period end (a date or a list of dates),
    one upsert per table."""
    periods = period_end if isinstance(period_end, list) else [period_end]
    inc, cf, bs = [], [], []
    for pe in periods:
        filed = pe + timedelta(days=30) if filing_date == "auto" else filing_date
        key = {"ticker": ticker, "period_end": pe, "frequency": frequency, "filing_date": filed}
        inc.append(
            {**key, "revenue": revenue, "gross_profit": gross_profit, "net_income": net_income}
        )
        cf.append({**key, "free_cash_flow": fcf})
        bs.append(
            {
                **key,
                "total_stockholder_equity": equity,
                "total_assets": assets,
                "total_liabilities": liabilities,
                "common_stock_shares_outstanding": shares,
            }
        )
    lake.upsert_income_statement(pd.DataFrame(inc))
    lake.upsert_cash_flow_statement(pd.DataFrame(cf))
    lake.upsert_balance_sheet(pd.DataFrame(bs))


@pytest.fixture
def two_names(lake):
    """GOOD: cheap, profitable, low leverage. BAD: expensive, thin margins,
    levered. Same share count; GOOD trades at 10, BAD at 50."""
    _quarter(lake, "GOOD.US", list(QUARTERS_2023))
    _quarter(
        lake,
        "BAD.US",
        list(QUARTERS_2023),
        gross_profit=10.0,
        net_income=1.0,
        fcf=0.5,
        equity=40.0,
        liabilities=360.0,
    )
    _prices(lake, "GOOD.US", 10.0)
    _prices(lake, "BAD.US", 50.0)
    return lake


# ---- surface ---------------------------------------------------------------


def test_satisfies_strategy_protocol_and_is_equity_only():
    s = QualityValue({})
    assert isinstance(s, Strategy)
    assert QualityValue.applicable_asset_classes == ("equity",)


def test_weights_and_top_k_are_tunable():
    names = {p.name for p in tunable_only(QualityValue.parameter_spec())}
    assert {
        "w_earnings_yield",
        "w_fcf_yield",
        "w_roe",
        "w_gross_margin",
        "w_low_leverage",
        "top_k",
    } <= names
    lag = next(p for p in QualityValue.parameter_spec() if p.name == "missing_filing_lag_days")
    assert not lag.tunable


# ---- features ---------------------------------------------------------------


def test_features_use_ttm_of_last_four_quarters(two_names):
    f = QualityValue({}).extract_features("GOOD.US", date(2024, 3, 1), two_names).values
    # market cap 10 * 100 = 1000; TTM NI 40, FCF 32, revenue 400, GP 160
    assert f["market_cap"] == pytest.approx(1000.0)
    assert f["earnings_yield"] == pytest.approx(0.04)
    assert f["fcf_yield"] == pytest.approx(0.032)
    assert f["roe"] == pytest.approx(40.0 / 200.0)
    assert f["gross_margin"] == pytest.approx(0.4)
    assert f["leverage"] == pytest.approx(0.5)


def test_better_business_at_a_lower_price_scores_higher(two_names):
    s = QualityValue({})
    as_of = date(2024, 3, 1)
    good = s.estimate_return("GOOD.US", as_of, two_names)
    bad = s.estimate_return("BAD.US", as_of, two_names)
    assert good is not None and good > 0
    assert bad is None or bad < good


def test_statement_is_invisible_before_its_filing_date(two_names):
    # Q1 2024 with a huge profit, filed 2024-05-10.
    _quarter(
        two_names, "GOOD.US", date(2024, 3, 31), net_income=500.0, filing_date=date(2024, 5, 10)
    )
    s = QualityValue({})
    before = s.extract_features("GOOD.US", date(2024, 5, 9), two_names).values
    on = s.extract_features("GOOD.US", date(2024, 5, 10), two_names).values
    assert before["earnings_yield"] == pytest.approx(0.04)  # still 2023 TTM
    assert on["earnings_yield"] == pytest.approx((10 + 10 + 10 + 500) / 1000.0)


def test_intraday_datetime_as_of_uses_its_calendar_day(two_names):
    _quarter(
        two_names, "GOOD.US", date(2024, 3, 31), net_income=500.0, filing_date=date(2024, 5, 10)
    )
    s = QualityValue({})
    f = s.extract_features("GOOD.US", datetime(2024, 5, 9, 23, 0), two_names).values
    assert f["earnings_yield"] == pytest.approx(0.04)


def test_intraday_as_of_does_not_see_a_filing_dated_the_same_day(two_names):
    """Filing dates carry no time: a report filed on 2024-05-10 may land
    after the close, so mid-session on 2024-05-10 it is not yet known. A
    daily as_of (read after the close) does see it."""
    _quarter(
        two_names, "GOOD.US", date(2024, 3, 31), net_income=500.0, filing_date=date(2024, 5, 10)
    )
    s = QualityValue({})
    mid = s.extract_features("GOOD.US", datetime(2024, 5, 10, 10, 0), two_names).values
    assert mid["earnings_yield"] == pytest.approx(0.04)
    after_close = s.extract_features("GOOD.US", date(2024, 5, 10), two_names).values
    assert after_close["earnings_yield"] == pytest.approx((10 + 10 + 10 + 500) / 1000.0)
    next_day = s.extract_features("GOOD.US", datetime(2024, 5, 11, 10, 0), two_names).values
    assert next_day["earnings_yield"] == pytest.approx((10 + 10 + 10 + 500) / 1000.0)


def test_intraday_as_of_prices_off_the_previous_completed_session(two_names):
    """Daily bars are stamped at midnight but close at the session end: at
    10:00 on 2024-03-01 that day's close is still in the future."""
    _prices(two_names, "GOOD.US", 20.0, start="2024-03-01", end="2024-03-01")
    s = QualityValue({})
    intraday = s.extract_features("GOOD.US", datetime(2024, 3, 1, 10, 0), two_names).values
    assert intraday["market_cap"] == pytest.approx(10.0 * 100)  # 2024-02-29 close
    late = s.extract_features("GOOD.US", datetime(2024, 3, 1, 23, 59), two_names).values
    assert late["market_cap"] == pytest.approx(10.0 * 100)
    # a daily as_of (a date, or the midnight bar stamp) is after that day's close
    assert s.extract_features("GOOD.US", date(2024, 3, 1), two_names).values[
        "market_cap"
    ] == pytest.approx(20.0 * 100)
    assert s.extract_features("GOOD.US", datetime(2024, 3, 1), two_names).values[
        "market_cap"
    ] == pytest.approx(20.0 * 100)


def test_missing_filing_date_waits_for_the_lag(lake):
    _quarter(lake, "X.US", list(QUARTERS_2023), filing_date=None)
    _prices(lake, "X.US", 10.0)
    s = QualityValue({"missing_filing_lag_days": 60})
    # Q4 2023 (period end 12-31) becomes visible 2024-02-29 with a 60-day lag.
    assert "earnings_yield" not in s.extract_features("X.US", date(2024, 2, 28), lake).values
    assert s.extract_features("X.US", date(2024, 2, 29), lake).values["earnings_yield"] == (
        pytest.approx(0.04)
    )


def test_falls_back_to_annual_when_quarters_are_incomplete(lake):
    _quarter(
        lake,
        "X.US",
        date(2023, 12, 31),
        frequency="A",
        revenue=400.0,
        gross_profit=100.0,
        net_income=30.0,
        fcf=20.0,
    )
    _quarter(lake, "X.US", date(2024, 3, 31))  # a single newer quarter: no TTM
    _prices(lake, "X.US", 10.0)
    f = QualityValue({}).extract_features("X.US", date(2024, 6, 1), lake).values
    assert f["earnings_yield"] == pytest.approx(30.0 / 1000.0)
    assert f["gross_margin"] == pytest.approx(0.25)
    # balance sheet: the newest available row (the Q1 2024 quarter)
    assert f["leverage"] == pytest.approx(0.5)


def test_prefers_annual_when_it_is_newer_than_the_ttm_window(lake):
    _quarter(
        lake, "X.US", [date(2022, 12, 31), date(2023, 3, 31), date(2023, 6, 30), date(2023, 9, 30)]
    )
    _quarter(lake, "X.US", date(2023, 12, 31), frequency="A", net_income=80.0)
    _prices(lake, "X.US", 10.0)
    f = QualityValue({}).extract_features("X.US", date(2024, 3, 1), lake).values
    assert f["earnings_yield"] == pytest.approx(0.08)


def test_stale_fundamentals_are_ignored(two_names):
    s = QualityValue({"max_statement_age_days": 400})
    assert s.estimate_return("GOOD.US", date(2024, 12, 1), two_names) is not None
    assert s.estimate_return("GOOD.US", date(2025, 3, 1), two_names) is None


def test_stale_price_means_no_signal(lake):
    """A delisted name keeps its last close forever; don't trade on it."""
    _quarter(lake, "GONE.US", list(QUARTERS_2023))
    _prices(lake, "GONE.US", 10.0, end="2024-02-01")
    s = QualityValue({})
    assert s.estimate_return("GONE.US", date(2024, 2, 1), lake) is not None
    assert s.estimate_return("GONE.US", date(2024, 3, 1), lake) is None


def test_negative_equity_drops_roe_but_does_not_crash(lake):
    _quarter(lake, "NEG.US", list(QUARTERS_2023), equity=-50.0, liabilities=450.0)
    _prices(lake, "NEG.US", 10.0)
    f = QualityValue({}).extract_features("NEG.US", date(2024, 3, 1), lake).values
    assert "roe" not in f
    assert f["leverage"] == pytest.approx(450.0 / 400.0)


@pytest.mark.parametrize("field", ["shares", "assets", "revenue"])
def test_zero_denominators_do_not_crash(lake, field):
    _quarter(lake, "Z.US", list(QUARTERS_2023), **{field: 0.0})
    _prices(lake, "Z.US", 10.0)
    s = QualityValue({})
    s.extract_features("Z.US", date(2024, 3, 1), lake)
    r = s.estimate_return("Z.US", date(2024, 3, 1), lake)
    if field == "shares":
        assert r is None  # no market cap, no value signal


def test_stale_share_counts_are_ignored(lake):
    _quarter(lake, "S.US", list(QUARTERS_2023), shares=None)
    lake.upsert_shares_outstanding(
        pd.DataFrame([{"ticker": "S.US", "date": date(2019, 12, 31), "shares": 200.0}])
    )
    _prices(lake, "S.US", 10.0)
    f = QualityValue({}).extract_features("S.US", date(2024, 3, 1), lake).values
    assert "market_cap" not in f


def test_gross_margin_from_zero_cost_of_revenue(lake):
    lake.upsert_income_statement(
        pd.DataFrame(
            [
                {
                    "ticker": "C.US",
                    "period_end": date(2023, 12, 31),
                    "frequency": "A",
                    "filing_date": date(2024, 2, 1),
                    "revenue": 100.0,
                    "cost_of_revenue": 0.0,
                    "net_income": 10.0,
                }
            ]
        )
    )
    f = QualityValue({}).extract_features("C.US", date(2024, 3, 1), lake).values
    assert f["gross_margin"] == pytest.approx(1.0)


def test_share_count_falls_back_to_the_shares_table_with_lag(lake):
    _quarter(lake, "S.US", list(QUARTERS_2023), shares=None)
    lake.upsert_shares_outstanding(
        pd.DataFrame([{"ticker": "S.US", "date": date(2023, 12, 31), "shares": 200.0}])
    )
    _prices(lake, "S.US", 10.0)
    s = QualityValue({"missing_filing_lag_days": 90})
    assert "market_cap" not in s.extract_features("S.US", date(2024, 3, 1), lake).values
    assert s.extract_features("S.US", date(2024, 3, 30), lake).values["market_cap"] == (
        pytest.approx(2000.0)
    )


def test_needs_both_a_value_and_a_quality_metric(lake):
    """Three quarters filed: no TTM earnings yet, only balance-sheet data.
    A low-leverage balance sheet alone must not make a pick."""
    _quarter(lake, "X.US", list(QUARTERS_2023[:3]), liabilities=40.0)
    _prices(lake, "X.US", 10.0)
    s = QualityValue({})
    f = s.extract_features("X.US", date(2024, 1, 15), lake).values
    assert f["low_leverage"] > 0 and "earnings_yield" not in f
    assert s.estimate_return("X.US", date(2024, 1, 15), lake) is None
    # value-only weighting still works once earnings exist
    _quarter(lake, "X.US", QUARTERS_2023[3], liabilities=40.0)
    value_only = QualityValue({"w_roe": 0.0, "w_gross_margin": 0.0, "w_low_leverage": 0.0})
    assert value_only.estimate_return("X.US", date(2024, 3, 1), lake) is not None


def test_no_statements_no_signal(lake):
    _prices(lake, "X.US", 10.0)
    s = QualityValue({})
    assert s.estimate_return("X.US", date(2024, 3, 1), lake) is None
    assert s.extract_features("X.US", date(2024, 3, 1), lake).values == {}
    assert s.estimate_return("X.US", date(2024, 3, 1), None) is None


def test_all_zero_weights_give_no_signal(two_names):
    zero = dict.fromkeys(
        ("w_earnings_yield", "w_fcf_yield", "w_roe", "w_gross_margin", "w_low_leverage"), 0.0
    )
    assert QualityValue(zero).estimate_return("GOOD.US", date(2024, 3, 1), two_names) is None


def test_min_score_filters_weak_names(two_names):
    s = QualityValue({"min_score": 0.99})
    assert s.estimate_return("GOOD.US", date(2024, 3, 1), two_names) is None


def test_reads_each_statement_once_per_ticker(two_names):
    class Counting:
        def __init__(self, inner):
            self.inner = inner
            self.calls = 0

        def __getattr__(self, name):
            attr = getattr(self.inner, name)
            if name.startswith("get_statement"):
                self.calls += 1
            return attr

    counting = Counting(two_names)
    s = QualityValue({})
    for d in pd.bdate_range("2024-02-01", "2024-04-30"):
        s.estimate_return("GOOD.US", d.date(), counting)
    assert counting.calls == 3


# ---- decide -----------------------------------------------------------------


def test_decide_buys_top_k_equal_weight_and_exits_the_rest():
    s = QualityValue({"top_k": 2})
    picks = [(0.09, "A.US"), (0.05, "B.US"), (0.03, "C.US")]
    portfolio = Portfolio(cash=1_000.0, positions={"OLD.US": 10.0})
    prices = {"A.US": 10.0, "B.US": 20.0, "C.US": 5.0, "OLD.US": 100.0}
    orders = s.decide(picks, portfolio, prices, date(2024, 3, 1))
    by = {(o.side, o.ticker): o for o in orders}
    assert set(by) == {("sell", "OLD.US"), ("buy", "A.US"), ("buy", "B.US")}
    assert by[("sell", "OLD.US")].quantity == 10.0
    # equity 2000, two slots of 1000 each
    assert by[("buy", "A.US")].quantity == pytest.approx(100.0)
    assert by[("buy", "B.US")].quantity == pytest.approx(50.0)
    assert orders[0].side == "sell"  # sells queue first to fund the buys


def test_decide_keeps_held_top_k_names_without_trading_them():
    s = QualityValue({"top_k": 2})
    portfolio = Portfolio(cash=500.0, positions={"A.US": 50.0})
    orders = s.decide(
        [(0.09, "A.US"), (0.05, "B.US")], portfolio, {"A.US": 10.0, "B.US": 10.0}, date(2024, 3, 1)
    )
    assert [(o.side, o.ticker) for o in orders] == [("buy", "B.US")]
    assert orders[0].quantity == pytest.approx(50.0)  # 1000 equity / 2 = 500


def test_decide_never_spends_more_than_it_has():
    s = QualityValue({"top_k": 3})
    portfolio = Portfolio(cash=100.0, positions={"A.US": 100.0})
    prices = {"A.US": 10.0, "B.US": 1.0, "C.US": 1.0}
    orders = s.decide(
        [(0.09, "A.US"), (0.05, "B.US"), (0.04, "C.US")], portfolio, prices, date(2024, 3, 1)
    )
    spent = sum(o.quantity * prices[o.ticker] for o in orders if o.side == "buy")
    assert spent <= 100.0 + 1e-9


def test_decide_skips_picks_without_a_price_and_exits_all_on_no_picks():
    s = QualityValue({"top_k": 1})
    portfolio = Portfolio(cash=100.0, positions={"A.US": 1.0})
    orders = s.decide([(0.1, "NOPX.US")], portfolio, {"A.US": 10.0}, date(2024, 3, 1))
    assert [(o.side, o.ticker) for o in orders] == [("sell", "A.US")]
    assert s.decide([], Portfolio(cash=100.0), {}, date(2024, 3, 1)) == []


def test_save_load_round_trip(tmp_path):
    s = QualityValue({"top_k": 7, "w_roe": 2.5})
    s.save(tmp_path / "a")
    loaded = QualityValue.load(tmp_path / "a")
    assert loaded.params == s.params

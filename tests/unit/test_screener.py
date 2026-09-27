"""The screener (roadmap 20.8): the metric registry, point-in-time metric
values from the lake, filters, sorting, and a rule universe that runs a
screen at each rebalance date."""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from stonks.screener import ScreenSpec, metric_ids, run_screen
from stonks.screener.data import ScreenData
from stonks.universes import UniverseDefinition, UniverseStore, refresh_universe
from tests.fixtures.screener import END, seed_market


@pytest.fixture
def market(lake):
    return seed_market(lake)


# ---- registry ----------------------------------------------------------------------


def test_metric_registry_names_price_and_fundamental_metrics():
    ids = metric_ids()
    for expected in (
        "price",
        "dollar_volume_20d",
        "return_1m",
        "return_12m",
        "volatility_3m",
        "from_high_52w",
        "market_cap",
        "pe_ratio",
        "pb_ratio",
        "ps_ratio",
        "dividend_yield",
        "net_margin",
        "roe",
        "debt_to_equity",
        "revenue_growth",
    ):
        assert expected in ids
    assert len(ids) == len(set(ids))


def test_spec_rejects_unknown_metrics_and_empty_bounds():
    with pytest.raises(ValidationError, match="unknown metric"):
        ScreenSpec(filters=[{"metric": "nope", "min": 1}])
    with pytest.raises(ValidationError, match="min or max"):
        ScreenSpec(filters=[{"metric": "price"}])
    with pytest.raises(ValidationError, match="min must be"):
        ScreenSpec(filters=[{"metric": "price", "min": 5, "max": 1}])
    with pytest.raises(ValidationError, match="unknown metric"):
        ScreenSpec(sort_by="nope")
    with pytest.raises(ValidationError):
        ScreenSpec(typo=1)


# ---- metric values -------------------------------------------------------------------


def test_price_metrics(market):
    data = ScreenData(market, ["AAA.US", "BBB.US", "CCC.US"], END)
    values = {m: data.metric(m) for m in ("price", "return_12m", "from_high_52w")}
    assert values["price"]["AAA.US"] == pytest.approx(20.0)
    assert values["return_12m"]["AAA.US"] > 0.5
    assert values["return_12m"]["BBB.US"] == pytest.approx(0.0)
    assert values["return_12m"]["CCC.US"] < 0
    assert values["from_high_52w"]["AAA.US"] == pytest.approx(0.0)
    assert values["from_high_52w"]["CCC.US"] < -0.4
    dv = data.metric("dollar_volume_20d")
    assert dv["BBB.US"] == pytest.approx(50.0 * 100_000)
    assert data.metric("volatility_3m")["BBB.US"] == pytest.approx(0.0)


def test_fundamentals_are_point_in_time(market):
    data = ScreenData(market, ["AAA.US", "BBB.US", "CCC.US"], END)
    # TTM net income is three 1M quarters and one 0.5M quarter: the 9e9
    # quarter was filed in 2025, after the screen date (P12)
    assert data.metric("pe_ratio")["AAA.US"] == pytest.approx(2e8 / 3.5e6)
    assert data.metric("pb_ratio")["AAA.US"] == pytest.approx(10.0)
    assert data.metric("ps_ratio")["AAA.US"] == pytest.approx(2e8 / 3.8e7)
    assert data.metric("net_margin")["AAA.US"] == pytest.approx(3.5e6 / 3.8e7)
    assert data.metric("roe")["AAA.US"] == pytest.approx(3.5e6 / 2e7)
    assert data.metric("debt_to_equity")["AAA.US"] == pytest.approx(0.5)
    assert data.metric("revenue_growth")["AAA.US"] == pytest.approx(3.8e7 / 3.2e7 - 1)
    assert data.metric("dividend_yield")["BBB.US"] == pytest.approx(2.0 / 50.0)
    # a loss has no P/E, and a name with no statements has no value at all
    assert "CCC.US" not in data.metric("pe_ratio")
    assert "BBB.US" not in data.metric("net_margin")
    later = ScreenData(market, ["AAA.US"], date(2025, 3, 1))
    assert later.metric("roe")["AAA.US"] > 100


def test_market_cap_falls_back_to_price_times_shares(market):
    market.con.execute("DELETE FROM market_cap_history")
    data = ScreenData(market, ["AAA.US"], END)
    assert data.metric("market_cap")["AAA.US"] == pytest.approx(20.0 * 1e6)


def test_a_dead_ticker_has_no_price(market):
    data = ScreenData(market, ["AAA.US"], date(2025, 6, 1))
    assert data.metric("price") == {}


# ---- screens ---------------------------------------------------------------------------


def test_base_filters_come_from_the_universe_rule(market):
    result = run_screen(market, ScreenSpec(sectors=["Tech"]), END)
    assert [r.ticker for r in result.rows] == ["AAA.US", "BBB.US"]
    assert result.candidates == 2
    assert result.rows[0].name == "Aaa Inc"


def test_filters_sort_and_limit(market):
    spec = ScreenSpec(filters=[{"metric": "return_12m", "min": 0.5}])
    assert [r.ticker for r in run_screen(market, spec, END).rows] == ["AAA.US"]
    spec = ScreenSpec(sort_by="price", descending=True, limit=2)
    result = run_screen(market, spec, END)
    assert [r.ticker for r in result.rows] == ["BBB.US", "AAA.US"]
    assert result.matched == 3 and result.truncated
    assert set(result.rows[0].values) == {"price"}
    spec = ScreenSpec(sort_by="price", descending=False, columns=["return_1m"])
    rows = run_screen(market, spec, END).rows
    assert [r.ticker for r in rows] == ["CCC.US", "AAA.US", "BBB.US"]
    assert set(rows[0].values) == {"price", "return_1m"}


def test_a_missing_value_fails_its_filter_and_sorts_last(market):
    spec = ScreenSpec(filters=[{"metric": "pe_ratio", "max": 100}])
    assert [r.ticker for r in run_screen(market, spec, END).rows] == ["AAA.US"]
    spec = ScreenSpec(sort_by="pe_ratio")
    rows = run_screen(market, spec, END).rows
    assert rows[0].ticker == "AAA.US"
    assert rows[1].values["pe_ratio"] is None


def test_universe_id_narrows_the_candidates(market):
    UniverseStore(market).save(
        UniverseDefinition(id="two", kind="list", spec={"tickers": ["BBB.US", "CCC.US"]})
    )
    refresh_universe(market, "two", as_of=END)
    result = run_screen(market, ScreenSpec(universe_id="two", sectors=["Tech"]), END)
    assert [r.ticker for r in result.rows] == ["BBB.US"]
    with pytest.raises(KeyError):
        run_screen(market, ScreenSpec(universe_id="ghost"), END)


def test_rule_universe_runs_the_screen_at_each_rebalance(market):
    UniverseStore(market).save(
        UniverseDefinition(
            id="rising",
            kind="rule",
            spec={
                "start": "2024-06-03",
                "end": "2024-12-31",
                "rebalance": "monthly",
                "filters": [{"metric": "return_3m", "min": 0.01}],
            },
        )
    )
    refresh_universe(market, "rising", as_of=END)
    tickers = set(market.get_universe_membership("rising")["ticker"])
    assert tickers == {"AAA.US"}


def test_rule_universe_top_n(market):
    UniverseStore(market).save(
        UniverseDefinition(
            id="top1",
            kind="rule",
            spec={"start": "2024-12-02", "sort_by": "price", "limit": 1},
        )
    )
    refresh_universe(market, "top1", as_of=END)
    assert set(market.get_universe_membership("top1")["ticker"]) == {"BBB.US"}

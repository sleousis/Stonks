"""Rate curves, dividend forecasts and the pricing market (roadmap 17.7).

Reference values come from QuantLib itself: a ``ZeroCurve`` with linear
interpolation on continuous zero rates for the curve, and
``AnalyticDividendEuropeanEngine`` (the escrowed dividend model) for
European prices with discrete dividends. The look-ahead tests shock data
dated after the pricing day and check that nothing moves (P12).
"""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import date, timedelta

import pytest
import QuantLib as ql

from stonks.core.options import OptionContract
from stonks.options.dividends import (
    DividendRecord,
    FlatDividendYield,
    KnownDividendForecast,
    NoDividends,
)
from stonks.options.market import PricingMarket
from stonks.options.pricing import pricing_model
from stonks.options.rates import (
    FlatRateCurve,
    RateCurveSettings,
    TreasuryRateCurve,
    bond_equivalent_to_continuous,
)

AS_OF = date(2025, 3, 3)
BS = pricing_model("black_scholes")
BAW = pricing_model("american_baw")

# tenors in whole days so a QuantLib curve can share the exact knots
T1, T2, T3 = 91 / 365, 365 / 365, 730 / 365
YIELDS = {T1: 4.30, T2: 4.10, T3: 3.95}  # percent, bond equivalent


def treasury(
    shock_after: float = 0.0, *, age_days: int = 0, settings: RateCurveSettings | None = None
) -> TreasuryRateCurve:
    """A curve with one observation per tenor on the pricing day (or
    ``age_days`` before it) and a shocked observation every day after it."""
    points: dict[float, list[tuple[date, float]]] = {}
    for tenor, y in YIELDS.items():
        rows = [
            (AS_OF - timedelta(days=age_days + 30), y - 0.5),
            (AS_OF - timedelta(days=age_days), y),
        ]
        rows += [(AS_OF + timedelta(days=d), y + shock_after) for d in range(1, 40)]
        points[tenor] = rows
    return TreasuryRateCurve(points, settings=settings)


def contract(expiry: date, right: str = "call", strike: float = 100.0, style: str = "european"):
    return OptionContract(
        underlying="X.US",
        expiry=expiry,
        strike=strike,
        right=right,
        style=style,  # type: ignore[arg-type]
    )


# ---- rates ----------------------------------------------------------------------------


def test_bond_equivalent_yield_converts_to_a_continuous_rate():
    assert bond_equivalent_to_continuous(4.0) == pytest.approx(2 * math.log(1.02))
    assert bond_equivalent_to_continuous(0.0) == 0.0


def test_flat_curve_is_the_same_rate_at_every_tenor():
    curve = FlatRateCurve(0.03)
    assert curve.zero_rate(AS_OF, 0.1) == curve.zero_rate(AS_OF, 5.0) == 0.03


def test_treasury_curve_matches_a_quantlib_zero_curve():
    curve = treasury()
    ql.Settings.instance().evaluationDate = ql.Date(3, 3, 2025)
    ref = ql.Date(3, 3, 2025)
    dates = [ref] + [ref + int(round(t * 365)) for t in YIELDS]
    rates = [bond_equivalent_to_continuous(YIELDS[T1])] + [
        bond_equivalent_to_continuous(y) for y in YIELDS.values()
    ]
    qc = ql.ZeroCurve(
        dates, rates, ql.Actual365Fixed(), ql.NullCalendar(), ql.Linear(), ql.Continuous
    )
    for days in (30, 91, 120, 200, 365, 500, 730):
        t = days / 365
        expected = qc.zeroRate(t, ql.Continuous).rate()
        assert curve.zero_rate(AS_OF, t) == pytest.approx(expected, abs=1e-12), days


def test_treasury_curve_is_flat_beyond_its_ends():
    curve = treasury()
    assert curve.zero_rate(AS_OF, 0.01) == pytest.approx(bond_equivalent_to_continuous(4.30))
    assert curve.zero_rate(AS_OF, 10.0) == pytest.approx(bond_equivalent_to_continuous(3.95))


def test_treasury_curve_uses_only_yields_known_on_the_pricing_day():
    base = treasury()
    shocked = treasury(shock_after=3.0)
    for t in (0.1, 0.5, 1.5, 3.0):
        assert shocked.zero_rate(AS_OF, t) == base.zero_rate(AS_OF, t)
    # the shock is visible the day after
    later = AS_OF + timedelta(days=1)
    assert shocked.zero_rate(later, 0.5) > base.zero_rate(later, 0.5)


def test_treasury_curve_falls_back_to_the_flat_rate_with_a_reason():
    empty = TreasuryRateCurve({}, settings=RateCurveSettings(fallback_rate=0.02))
    assert empty.zero_rate(AS_OF, 0.5) == 0.02
    assert empty.fallback_reason(AS_OF) == "no treasury yields known"
    before = treasury()
    early = AS_OF - timedelta(days=400)
    assert before.zero_rate(early, 0.5) == 0.0
    assert before.fallback_reason(early) == "no treasury yields known"
    assert before.fallback_reason(AS_OF) is None


def test_stale_yields_are_dropped():
    fresh = treasury(age_days=5, settings=RateCurveSettings(max_age_days=10))
    assert fresh.fallback_reason(AS_OF) is None
    stale = treasury(age_days=20, settings=RateCurveSettings(max_age_days=10, fallback_rate=0.01))
    assert stale.zero_rate(AS_OF, 0.5) == 0.01
    assert "stale" in (stale.fallback_reason(AS_OF) or "")


def test_the_fallback_is_logged(monkeypatch):
    from stonks.options import rates

    seen: list[tuple[str, dict]] = []

    class Log:
        def warning(self, event, **kw):
            seen.append((event, kw))

    monkeypatch.setattr(rates, "_log", Log())
    curve = TreasuryRateCurve({})
    curve.zero_rate(AS_OF, 0.5)
    curve.zero_rate(AS_OF, 1.0)  # once per day, not per call
    assert [e for e, _ in seen] == ["options.rate_curve.flat_fallback"]
    assert seen[0][1]["reason"] == "no treasury yields known"


def test_settings_reject_bad_tenors():
    with pytest.raises(ValueError):
        RateCurveSettings(tickers={"US3M.GBOND": 0.0})


def test_treasury_curve_from_lake(lake):
    import pandas as pd

    lake.upsert_bond_yields(
        pd.DataFrame(
            {
                "ticker": ["US3M.GBOND", "US10Y.GBOND", "US10Y.GBOND"],
                "date": [AS_OF, AS_OF, AS_OF + timedelta(days=1)],
                "yield_to_maturity": [4.3, 4.0, 9.0],
                "clean_price": [None, None, None],
            }
        )
    )
    settings = RateCurveSettings(tickers={"US3M.GBOND": 0.25, "US10Y.GBOND": 10.0})
    curve = TreasuryRateCurve.from_lake(lake, settings)
    assert curve.zero_rate(AS_OF, 0.25) == pytest.approx(bond_equivalent_to_continuous(4.3))
    assert curve.zero_rate(AS_OF, 10.0) == pytest.approx(bond_equivalent_to_continuous(4.0))


# ---- dividends -------------------------------------------------------------------------


def div(ex: date, amount: float, declared: date | None = None) -> DividendRecord:
    return DividendRecord(ex_date=ex, amount=amount, declaration_date=declared)


QUARTERLY = [div(AS_OF - timedelta(days=d), 0.5) for d in (20, 111, 202, 293)]


def test_no_dividends_and_flat_yield():
    c = contract(AS_OF + timedelta(days=180))
    flat0 = FlatRateCurve(0.0)
    assert NoDividends().dividend_yield(c.underlying, AS_OF, c.expiry, 100.0, flat0) == 0.0
    assert (
        FlatDividendYield(0.02).dividend_yield(c.underlying, AS_OF, c.expiry, 100.0, flat0) == 0.02
    )


def test_known_future_dividends_are_only_those_declared_by_the_pricing_day():
    expiry = AS_OF + timedelta(days=120)
    records = [
        div(AS_OF + timedelta(days=30), 0.6, declared=AS_OF - timedelta(days=5)),
        div(AS_OF + timedelta(days=60), 0.7, declared=AS_OF + timedelta(days=1)),  # not yet
        div(AS_OF + timedelta(days=90), 0.8, declared=None),  # unknown until ex-date
        div(AS_OF, 9.9, declared=AS_OF - timedelta(days=30)),  # already ex: in the spot
    ]
    fc = KnownDividendForecast({"X.US": records})
    assert fc.known_dividends("X.US", AS_OF, expiry) == [(AS_OF + timedelta(days=30), 0.6)]


def test_trailing_yield_when_nothing_is_declared():
    fc = KnownDividendForecast({"X.US": QUARTERLY})
    expiry = AS_OF + timedelta(days=200)
    q = fc.dividend_yield("X.US", AS_OF, expiry, 100.0, FlatRateCurve(0.0))
    assert q == pytest.approx(math.log(1 + 2.0 / 100.0))


def test_known_dividends_price_like_quantlibs_escrowed_dividend_model():
    """Black-Scholes with the equivalent yield equals QuantLib's analytic
    European engine with discrete dividends on a zero curve."""
    curve = treasury()
    ex1, ex2 = AS_OF + timedelta(days=40), AS_OF + timedelta(days=131)
    records = [
        div(ex1, 0.75, declared=AS_OF - timedelta(days=10)),
        div(ex2, 0.80, declared=AS_OF - timedelta(days=1)),
    ]
    market = PricingMarket(curve, KnownDividendForecast({"X.US": records}, trailing_days=0))
    expiry = AS_OF + timedelta(days=150)
    spot, vol = 102.0, 0.27

    ref = ql.Date(3, 3, 2025)
    ql.Settings.instance().evaluationDate = ref
    dc = ql.Actual365Fixed()
    dates = [ref] + [ref + int(round(t * 365)) for t in YIELDS]
    rates = [bond_equivalent_to_continuous(YIELDS[T1])] + [
        bond_equivalent_to_continuous(y) for y in YIELDS.values()
    ]
    rf = ql.YieldTermStructureHandle(
        ql.ZeroCurve(dates, rates, dc, ql.NullCalendar(), ql.Linear(), ql.Continuous)
    )
    process = ql.BlackScholesMertonProcess(
        ql.QuoteHandle(ql.SimpleQuote(spot)),
        ql.YieldTermStructureHandle(ql.FlatForward(ref, 0.0, dc)),
        rf,
        ql.BlackVolTermStructureHandle(ql.BlackConstantVol(ref, ql.NullCalendar(), vol, dc)),
    )
    div_dates = [ref + (ex1 - AS_OF).days, ref + (ex2 - AS_OF).days]
    schedule = ql.DividendVector(div_dates, [0.75, 0.80])
    engine = ql.AnalyticDividendEuropeanEngine(process, schedule)
    for right, strike in (("call", 95.0), ("call", 105.0), ("put", 100.0), ("put", 110.0)):
        option = ql.VanillaOption(
            ql.PlainVanillaPayoff(ql.Option.Call if right == "call" else ql.Option.Put, strike),
            ql.EuropeanExercise(ref + (expiry - AS_OF).days),
        )
        option.setPricingEngine(engine)
        c = contract(expiry, right, strike)
        ours = BS.price(market.inputs(c, AS_OF, spot=spot, vol=vol))
        assert ours == pytest.approx(option.NPV(), abs=1e-9), (right, strike)


def test_trailing_yield_covers_the_time_after_the_last_known_ex_date():
    ex = AS_OF + timedelta(days=30)
    records = [*QUARTERLY, div(ex, 0.5, declared=AS_OF - timedelta(days=3))]
    fc = KnownDividendForecast({"X.US": records})
    expiry = AS_OF + timedelta(days=365)
    t = 365 / 365
    q = fc.dividend_yield("X.US", AS_OF, expiry, 100.0, FlatRateCurve(0.0))
    q_known = -math.log(1 - 0.5 / 100.0) / t
    q_trail = math.log(1 + 2.0 / 100.0)
    assert q == pytest.approx(q_known + q_trail * (t - 30 / 365) / t)


def test_dividends_worth_more_than_the_spot_are_capped():
    records = [div(AS_OF + timedelta(days=10), 500.0, declared=AS_OF)]
    fc = KnownDividendForecast({"X.US": records})
    q = fc.dividend_yield("X.US", AS_OF, AS_OF + timedelta(days=30), 100.0, FlatRateCurve(0.0))
    assert math.isfinite(q) and q > 0


def test_dividend_forecast_from_lake(lake):
    import pandas as pd

    lake.upsert_dividends(
        pd.DataFrame(
            {
                "ticker": ["X.US", "X.US"],
                "ex_date": [AS_OF - timedelta(days=20), AS_OF + timedelta(days=20)],
                "amount": [0.5, 0.6],
                "currency": ["USD", "USD"],
                "pay_date": [None, None],
                "record_date": [None, None],
                "declaration_date": [AS_OF - timedelta(days=50), AS_OF - timedelta(days=10)],
            }
        )
    )
    fc = KnownDividendForecast.from_lake(lake, ["X.US", "Y.US"])
    assert fc.known_dividends("X.US", AS_OF, AS_OF + timedelta(days=60)) == [
        (AS_OF + timedelta(days=20), 0.6)
    ]
    assert fc.known_dividends("Y.US", AS_OF, AS_OF + timedelta(days=60)) == []


# ---- the pricing market -----------------------------------------------------------------


def test_flat_market_matches_the_old_flat_inputs():
    c = contract(AS_OF + timedelta(days=90))
    inputs = PricingMarket.flat(0.03, 0.01).inputs(c, AS_OF, spot=100.0, vol=0.2)
    assert (inputs.rate, inputs.dividend_yield, inputs.time) == (0.03, 0.01, 90 / 365)


def test_market_picks_the_curve_rate_at_each_expiry():
    market = PricingMarket(treasury(), NoDividends())
    short = market.inputs(contract(AS_OF + timedelta(days=91)), AS_OF, spot=100.0, vol=0.2)
    long = market.inputs(contract(AS_OF + timedelta(days=730)), AS_OF, spot=100.0, vol=0.2)
    assert short.rate == pytest.approx(bond_equivalent_to_continuous(4.30))
    assert long.rate == pytest.approx(bond_equivalent_to_continuous(3.95))


def test_expired_contract_gets_no_dividend_yield():
    market = PricingMarket(treasury(), KnownDividendForecast({"X.US": QUARTERLY}))
    inputs = market.inputs(contract(AS_OF), AS_OF, spot=100.0, vol=0.2)
    assert inputs.time == 0 and inputs.dividend_yield == 0.0


def test_prices_ignore_data_dated_after_the_pricing_day():
    """Shock every yield after the pricing day and every dividend not yet
    declared: no price, Greek or implied vol changes (P12)."""
    future_declared = [
        div(AS_OF + timedelta(days=d), 0.5, declared=AS_OF + timedelta(days=d - 30))
        for d in (40, 131)
    ]
    shocked_future = [
        div(AS_OF + timedelta(days=d), 5.0, declared=AS_OF + timedelta(days=d - 30))
        for d in (40, 131)
    ]
    base = PricingMarket(
        treasury(), KnownDividendForecast({"X.US": [*QUARTERLY, *future_declared]})
    )
    shocked = PricingMarket(
        treasury(shock_after=2.5),
        KnownDividendForecast({"X.US": [*QUARTERLY, *shocked_future]}),
    )
    for style, model in (("european", BS), ("american", BAW)):
        for right in ("call", "put"):
            c = contract(AS_OF + timedelta(days=150), right, 100.0, style)
            a = base.inputs(c, AS_OF, spot=101.0, vol=0.3)
            b = shocked.inputs(c, AS_OF, spot=101.0, vol=0.3)
            assert a == b
            assert model.price(a) == model.price(b)
            assert model.greeks(a) == model.greeks(b)
            p = model.price(a)
            assert model.implied_vol(a, p) == model.implied_vol(b, p)


def test_a_dividend_declared_before_the_pricing_day_does_move_the_price():
    declared = [div(AS_OF + timedelta(days=40), 2.0, declared=AS_OF - timedelta(days=1))]
    with_div = PricingMarket(treasury(), KnownDividendForecast({"X.US": declared}, trailing_days=0))
    without = PricingMarket(treasury(), NoDividends())
    c = contract(AS_OF + timedelta(days=150))
    assert BS.price(with_div.inputs(c, AS_OF, spot=100.0, vol=0.3)) < BS.price(
        without.inputs(c, AS_OF, spot=100.0, vol=0.3)
    )


# ---- the market reaches chains, Greeks, implied vol, selection and risk -------------------


def _priced_quote(market: PricingMarket, vol: float = 0.3):
    from stonks.options.chain import OptionQuote

    c = contract(AS_OF + timedelta(days=150), "call", 100.0, "european")
    price = BS.price(market.inputs(c, AS_OF, spot=101.0, vol=vol))
    return OptionQuote(c, AS_OF, bid=price, ask=price, underlying_price=101.0)


def rich_market() -> PricingMarket:
    declared = [div(AS_OF + timedelta(days=40), 1.5, declared=AS_OF - timedelta(days=1))]
    return PricingMarket(treasury(), KnownDividendForecast({"X.US": declared}))


def test_analyze_solves_implied_vol_and_greeks_in_the_market():
    from stonks.options.analytics import analyze, analyze_chain
    from stonks.options.chain import ChainSnapshot

    market = rich_market()
    quote = _priced_quote(market)
    a = analyze(quote, 101.0, market=market)
    assert a.iv == pytest.approx(0.3, abs=1e-7)
    assert a.greeks == BS.greeks(market.inputs(quote.contract, AS_OF, spot=101.0, vol=a.iv))
    # a flat market reads the same price as a different vol
    assert analyze(quote, 101.0).iv != pytest.approx(0.3, abs=1e-4)
    chain = ChainSnapshot("X.US", AS_OF, (quote,), spot=101.0)
    assert analyze_chain(chain, market=market)[quote.contract_id].iv == a.iv


def test_model_mark_prices_in_the_market():
    from stonks.options.analytics import model_mark

    market = rich_market()
    c = contract(AS_OF + timedelta(days=150))
    assert model_mark(c, AS_OF, 101.0, 0.3, market=market) == BS.price(
        market.inputs(c, AS_OF, spot=101.0, vol=0.3)
    )


def test_selector_delta_uses_the_market():
    from stonks.options.selector import LegSelector

    market = rich_market()
    quote = _priced_quote(market)
    delta = LegSelector(market=market).delta(quote, 101.0)
    expected = BS.greeks(market.inputs(quote.contract, AS_OF, spot=101.0, vol=0.3)).delta
    assert delta == pytest.approx(expected, abs=1e-6)


def test_risk_view_reprices_scenarios_in_the_market():
    from stonks.options.analytics import risk_view
    from stonks.options.chain import ChainSnapshot
    from stonks.options.risk import risk_based_requirement

    market = rich_market()
    quote = _priced_quote(market)
    chain = ChainSnapshot("X.US", AS_OF, (quote,), spot=101.0)
    view = risk_view(AS_OF, {"X.US": chain}, {"X.US": 101.0}, market=market)
    assert view.market is market
    assert view.ivs[quote.contract_id] == pytest.approx(0.3, abs=1e-7)
    flat = replace(view, market=None)
    assert flat.pricing().rates.zero_rate(AS_OF, 1.0) == 0.0
    short = {quote.contract_id: -1.0}
    assert risk_based_requirement(short, view) != risk_based_requirement(short, flat)

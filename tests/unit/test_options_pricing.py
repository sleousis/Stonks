"""Option pricing (roadmap 17.2) against published values.

Sources: Hull, *Options, Futures and Other Derivatives* (the 42/40 example
and the 49/50 Greeks table), Haug, *The Complete Guide to Option Pricing
Formulas* (Merton 1973 with a dividend yield, Black 1976), and
Barone-Adesi and Whaley (1987) Table I for American calls.
"""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import date

import pytest

from stonks.core.options import OptionContract
from stonks.options.pricing import (
    ZERO_GREEKS,
    Greeks,
    PricingInputs,
    PricingModel,
    default_model_for,
    inputs_for,
    pricing_model,
    pricing_models,
    register_pricing_model,
)
from stonks.options.pricing.base import finite_difference_greeks, solve_implied_vol
from stonks.options.surface import SurfacePoint, VolSurface

BS = pricing_model("black_scholes")
B76 = pricing_model("black76")
BAW = pricing_model("american_baw")
BJS = pricing_model("american_bjerksund")
CRR = pricing_model("american_binomial")


def inp(right="call", spot=42.0, strike=40.0, time=0.5, rate=0.1, q=0.0, vol=0.2):
    return PricingInputs(right, spot, strike, time, rate, q, vol)


def test_registry_lists_every_model():
    assert set(pricing_models()) >= {
        "black_scholes",
        "black76",
        "american_baw",
        "american_bjerksund",
        "american_binomial",
    }
    with pytest.raises(ValueError, match="unknown pricing model"):
        pricing_model("nope")


def test_registry_refuses_a_second_model_with_the_same_name():
    class Clash(PricingModel):
        name = "black_scholes"
        style = "european"

        def price(self, inputs):  # pragma: no cover - never called
            return 0.0

    with pytest.raises(ValueError, match="already registered"):
        register_pricing_model(Clash)


def test_hull_black_scholes_example():
    # Hull: S=42, K=40, r=10%, sigma=20%, T=0.5 -> c=4.76, p=0.81
    assert BS.price(inp()) == pytest.approx(4.7594, abs=1e-4)
    assert BS.price(inp("put")) == pytest.approx(0.8086, abs=1e-4)


def test_haug_merton_put_with_dividend_yield():
    # Haug: S=100, K=95, T=0.5, r=10%, q=5%, sigma=20% -> put 2.4648
    assert BS.price(inp("put", 100, 95, 0.5, 0.1, 0.05, 0.2)) == pytest.approx(2.4648, abs=1e-4)


@pytest.mark.parametrize(
    ("spot", "strike", "time", "r", "q", "vol"),
    [
        (42, 40, 0.5, 0.1, 0.0, 0.2),
        (100, 95, 0.5, 0.1, 0.05, 0.2),
        (100, 130, 2.0, 0.03, 0.01, 0.45),
    ],
)
def test_put_call_parity(spot, strike, time, r, q, vol):
    c = BS.price(inp("call", spot, strike, time, r, q, vol))
    p = BS.price(inp("put", spot, strike, time, r, q, vol))
    assert c - p == pytest.approx(spot * math.exp(-q * time) - strike * math.exp(-r * time))


def test_hull_greeks_table():
    # Hull: S=49, K=50, r=5%, sigma=20%, T=0.3846 (20 weeks)
    g = BS.greeks(inp("call", 49, 50, 0.3846, 0.05, 0.0, 0.2))
    assert g.delta == pytest.approx(0.522, abs=1e-3)
    assert g.gamma == pytest.approx(0.066, abs=1e-3)
    assert g.vega == pytest.approx(12.1, abs=0.05)
    assert g.theta == pytest.approx(-4.31, abs=0.01)
    assert g.rho == pytest.approx(8.91, abs=0.01)
    assert g.vega_per_point == pytest.approx(0.121, abs=1e-3)
    assert g.theta_per_day == pytest.approx(-4.31 / 365, abs=1e-4)


def test_analytic_greeks_match_finite_differences():
    for right in ("call", "put"):
        x = inp(right, 100, 105, 0.75, 0.04, 0.02, 0.3)
        a, n = BS.greeks(x), finite_difference_greeks(BS.price, x)
        assert a.delta == pytest.approx(n.delta, abs=1e-4)
        assert a.gamma == pytest.approx(n.gamma, rel=1e-3)
        assert a.vega == pytest.approx(n.vega, rel=1e-4)
        assert a.rho == pytest.approx(n.rho, rel=1e-3)
        assert a.theta == pytest.approx(n.theta, rel=0.02)


def test_black76_haug_example():
    # Haug: F=19, K=19, T=0.75, r=10%, sigma=28% -> call = put = 1.7011
    x = inp("call", 19, 19, 0.75, 0.1, 0.0, 0.28)
    assert B76.price(x) == pytest.approx(1.7011, abs=1e-4)
    assert B76.price(replace(x, right="put")) == pytest.approx(1.7011, abs=1e-4)
    # the dividend yield is ignored: the carry is in the futures price
    assert B76.price(replace(x, dividend_yield=0.05)) == pytest.approx(1.7011, abs=1e-4)


def test_black76_greeks_match_finite_differences():
    x = inp("put", 50, 55, 0.5, 0.05, 0.0, 0.25)
    a, n = B76.greeks(x), finite_difference_greeks(B76.price, x)
    for field in ("delta", "gamma", "vega", "rho"):
        assert getattr(a, field) == pytest.approx(getattr(n, field), rel=2e-3, abs=1e-5)
    assert a.theta == pytest.approx(n.theta, rel=0.02)


@pytest.mark.parametrize(
    ("spot", "time", "vol", "expected"),
    [
        (100, 0.1, 0.15, 1.8771),
        (110, 0.1, 0.15, 10.0089),
        (100, 0.5, 0.15, 4.0842),
        (110, 0.5, 0.15, 10.8087),
    ],
)
def test_barone_adesi_whaley_table(spot, time, vol, expected):
    # BAW (1987) Table I calls: K=100, r=10%, cost of carry 0 (q=10%)
    assert BAW.price(inp("call", spot, 100, time, 0.1, 0.1, vol)) == pytest.approx(
        expected, abs=3e-3
    )


def test_american_bounds_and_early_exercise():
    x = inp("put", 100, 110, 1.0, 0.08, 0.0, 0.25)
    euro = BS.price(x)
    for model in (BAW, BJS, CRR):
        assert model.price(x) >= euro
        assert model.price(x) >= x.intrinsic
    assert BAW.price(x) == pytest.approx(CRR.price(x), rel=0.01)
    # an American call on a stock with no dividends is never exercised early
    call = replace(x, right="call")
    assert CRR.price(call) == pytest.approx(BS.price(call), rel=2e-3)


def test_american_fractional_tenor_is_priced_exactly():
    # a half day of difference moves the price, rounding does not hide it
    a = BAW.price(inp("put", 100, 100, 0.5, 0.05, 0.0, 0.2))
    b = BAW.price(inp("put", 100, 100, 0.5 + 0.5 / 365, 0.05, 0.0, 0.2))
    assert b > a


def test_american_greeks_are_sane():
    g = BAW.greeks(inp("put", 100, 100, 0.5, 0.05, 0.0, 0.2))
    assert -1 < g.delta < 0 and g.gamma > 0 and g.vega > 0 and g.theta < 0 and g.rho < 0


def test_expired_and_zero_vol_prices():
    assert BAW.price(inp("put", 90, 100, 0.0)) == 10.0
    assert BS.price(inp("call", 110, 100, 0.0)) == 10.0
    assert BS.price(inp("call", 100, 100, 1.0, 0.05, 0.0, 0.0)) == pytest.approx(
        100 - 100 * math.exp(-0.05)
    )
    # Greeks still come back at expiry
    g = BS.greeks(inp("call", 110, 100, 0.0))
    assert g.delta == pytest.approx(1.0)


@pytest.mark.parametrize("model", [BS, B76, BAW, CRR], ids=lambda m: m.name)
@pytest.mark.parametrize("right", ["call", "put"])
def test_implied_vol_round_trips(model, right):
    x = inp(right, 100, 95, 0.4, 0.03, 0.0, 0.33)
    assert model.implied_vol(x, model.price(x)) == pytest.approx(0.33, abs=1e-5)


def test_implied_vol_is_none_outside_the_bounds():
    x = inp("call", 100, 90, 0.5, 0.05, 0.0)
    assert BS.implied_vol(x, 1.0) is None  # below intrinsic
    assert BS.implied_vol(x, 150.0) is None  # above the spot
    assert BS.implied_vol(x, 0.0) is None
    assert BS.implied_vol(x, float("nan")) is None
    assert BS.implied_vol(replace(x, time=0.0), 10.0) is None
    assert BAW.implied_vol(x, 1.0) is None
    assert BAW.implied_vol(x, 150.0) is None


def test_generic_solver_edges():
    x = inp("call", 100, 200, 0.1, 0.0, 0.0)
    # a deep out-of-the-money option priced at the floor solves to the minimum vol
    assert solve_implied_vol(BS.price, x, BS.price(x.with_vol(1e-4)) + 1e-12) == 1e-4


def test_inputs_validation():
    with pytest.raises(ValueError):
        inp(spot=0.0)
    with pytest.raises(ValueError):
        inp(time=-1.0)
    with pytest.raises(ValueError):
        inp(vol=-0.1)
    with pytest.raises(ValueError):
        inp(rate=float("inf"))
    with pytest.raises(ValueError):
        PricingInputs("fwd", 1, 1, 1)  # type: ignore[arg-type]


def test_inputs_for_contract_and_default_models():
    c = OptionContract("AAPL.US", date(2026, 1, 16), 150.0, "call")
    x = inputs_for(c, date(2025, 1, 16), spot=140.0, vol=0.3, rate=0.04)
    assert (x.time, x.strike, x.spot, x.right) == (1.0, 150.0, 140.0, "call")
    assert default_model_for(c).name == "american_baw"
    euro = replace(c, style="european")
    assert default_model_for(euro).name == "black_scholes"


def test_greeks_arithmetic():
    g = Greeks(0.5, 0.1, 10.0, -3.0, 5.0)
    assert g.scaled(-200) == Greeks(-100.0, -20.0, -2000.0, 600.0, -1000.0)
    assert g + ZERO_GREEKS == g


def test_vol_surface_interpolation():
    pts = [
        SurfacePoint(0.25, -0.1, 0.30),
        SurfacePoint(0.25, 0.0, 0.25),
        SurfacePoint(0.25, 0.1, 0.22),
        SurfacePoint(1.0, 0.0, 0.20),
        SurfacePoint(0.5, 0.0, float("nan")),
        SurfacePoint(0.0, 0.0, 0.5),
    ]
    s = VolSurface(pts)
    assert s.times == [0.25, 1.0]
    assert s.vol(0.25, -0.05) == pytest.approx(0.275)
    assert s.vol(0.25, 0.5) == pytest.approx(0.22)  # flat beyond the wings
    assert s.vol(0.1) == pytest.approx(0.25)
    assert s.vol(2.0) == pytest.approx(0.20)
    # total variance is linear in time
    w = 0.25**2 * 0.25 + (0.20**2 * 1.0 - 0.25**2 * 0.25) * (0.5 - 0.25) / 0.75
    assert s.vol(0.5) == pytest.approx(math.sqrt(w / 0.5))
    assert s.atm_term_structure() == [(0.25, 0.25), (1.0, 0.20)]
    with pytest.raises(ValueError):
        VolSurface([])

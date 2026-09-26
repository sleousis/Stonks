"""Built-in portfolio constructors, hand-checked, plus registry-driven
properties that cover every registered constructor automatically."""

from __future__ import annotations

import math
from datetime import date

import numpy as np
import pandas as pd
import pytest

from stonks.core.types import Portfolio
from stonks.portfolio.base import ConstructionInput, constructor_names, get_constructor
from stonks.portfolio.constructors import diversification_multiplier


def _inp(signals, *, vols=None, prices=None, **kw) -> ConstructionInput:
    tickers = {t for s in signals.values() for t in s}
    return ConstructionInput(
        signals=signals,
        portfolio=Portfolio(cash=1000.0),
        prices=prices if prices is not None else dict.fromkeys(tickers, 10.0),
        vols_annual=vols or {},
        as_of=date(2026, 1, 2),
        **kw,
    )


# --- single_winner: today's tick decision -------------------------------------


@pytest.mark.parametrize(
    ("signals", "winner", "picks"),
    [
        # BuyAndHold's raw 1.0 beats every realistic score: it takes the book
        (
            {"bh": {"SPY": 1.0}, "mom": {"AAPL": 0.3, "MSFT": 0.5}},
            "bh",
            [(1.0, "SPY")],
        ),
        (
            {"bh": {"SPY": 0.2}, "mom": {"AAPL": 0.3, "MSFT": 0.5}},
            "mom",
            [(0.5, "MSFT"), (0.3, "AAPL")],
        ),
        # ties keep ranking order (registry order, then universe order)
        ({"a": {"X": 0.5}, "b": {"Y": 0.5}}, "a", [(0.5, "X")]),
        # scores at or below the threshold never rank
        ({"a": {"X": 0.0, "Y": -1.0}, "b": {"Z": 0.1}}, "b", [(0.1, "Z")]),
    ],
)
def test_single_winner_reproduces_todays_tick_decision(signals, winner, picks) -> None:
    book = get_constructor("single_winner").target_weights(_inp(signals))
    assert book.meta["winner_strategy_id"] == winner
    assert book.meta["picks"] == picks
    tickers = [t for _, t in picks]
    assert book.weights == pytest.approx(dict.fromkeys(tickers, 1.0 / len(tickers)))
    assert all(book.attribution[t] == {winner: 1.0} for t in tickers)


def test_single_winner_with_nothing_ranked_is_empty() -> None:
    book = get_constructor("single_winner").target_weights(_inp({"a": {"X": -1.0}}))
    assert book.weights == {}
    assert book.meta["winner_strategy_id"] is None


def test_single_winner_uses_raw_signals() -> None:
    assert get_constructor("single_winner").signal_method == "raw"


# --- equal weight -------------------------------------------------------------


def test_equal_weight_top_n() -> None:
    signals = {"s": {"A": 1.0, "B": 3.0, "C": 2.0, "D": -1.0}}
    book = get_constructor("equal_weight_top_n", n=2).target_weights(_inp(signals))
    assert book.weights == pytest.approx({"B": 0.5, "C": 0.5})


def test_equal_weight_skips_non_positive_and_untradable() -> None:
    signals = {"s": {"A": 1.0, "B": 0.0, "C": 2.0}}
    book = get_constructor("equal_weight_top_n").target_weights(
        _inp(signals, prices={"A": 10.0, "B": 10.0})
    )
    assert book.weights == pytest.approx({"A": 1.0})


def test_two_strategies_both_get_capital_and_attribution_sums_to_one() -> None:
    signals = {"bh": {"SPY": 1.0}, "qv": {"A": 1.5, "B": 0.5, "SPY": 0.5}}
    book = get_constructor("equal_weight_top_n", n=3).target_weights(_inp(signals))
    assert set(book.weights) == {"SPY", "A", "B"}
    assert book.attribution["SPY"] == pytest.approx({"bh": 2 / 3, "qv": 1 / 3})
    for shares in book.attribution.values():
        assert sum(shares.values()) == pytest.approx(1.0)


# --- inverse vol --------------------------------------------------------------


def test_inverse_vol_weights_are_proportional_to_one_over_sigma() -> None:
    signals = {"s": {"A": 1.0, "B": 2.0, "C": 3.0}}
    vols = {"A": 0.1, "B": 0.2, "C": 0.4}
    book = get_constructor("inverse_vol").target_weights(_inp(signals, vols=vols))
    assert book.weights == pytest.approx({"A": 10 / 17.5, "B": 5 / 17.5, "C": 2.5 / 17.5})


def test_inverse_vol_top_n_and_missing_vol() -> None:
    signals = {"s": {"A": 1.0, "B": 2.0, "C": 3.0, "D": 4.0}}
    vols = {"A": 0.1, "B": 0.2, "C": 0.4}  # D has no vol: skipped
    book = get_constructor("inverse_vol", top_n=2).target_weights(_inp(signals, vols=vols))
    assert book.weights == pytest.approx({"B": 2 / 3, "C": 1 / 3})


# --- vol target ---------------------------------------------------------------

VOLS = {"X": 0.2, "Y": 0.4}


def test_vol_target_two_assets_by_hand() -> None:
    # w = tau * IDM * iw * F / 10 / sigma = 0.1 * 1 * 0.5 * F / 10 / sigma
    book = get_constructor("vol_target", tau=0.1, idm=1.0).target_weights(
        _inp({"s": {"X": 10.0, "Y": 20.0}}, vols=VOLS)
    )
    assert book.weights == pytest.approx({"X": 0.25, "Y": 0.25})
    assert book.meta["fdm"] == 1.0 and book.meta["idm"] == 1.0


def test_vol_target_unit_instrument_weight_matches_spec_formula() -> None:
    # w = tau * IDM * F / 10 / sigma
    book = get_constructor(
        "vol_target", tau=0.05, idm=1.0, instrument_weight="unit"
    ).target_weights(_inp({"s": {"X": 10.0, "Y": 20.0}}, vols=VOLS))
    assert book.weights == pytest.approx({"X": 0.25, "Y": 0.25})


def _uncorrelated(n: int = 32) -> pd.DataFrame:
    a = np.tile([1.0, -1.0], n // 2)
    b = np.tile([1.0, 1.0, -1.0, -1.0], n // 4)
    return pd.DataFrame({"a": a, "b": b})


def test_vol_target_fdm_from_uncorrelated_forecasts() -> None:
    signals = {"a": {"X": 10.0, "Y": 10.0}, "b": {"X": 10.0, "Y": 0.0}}
    book = get_constructor("vol_target", tau=0.1, idm=1.0).target_weights(
        _inp(signals, vols=VOLS, forecast_history=_uncorrelated())
    )
    fdm = math.sqrt(2)
    assert book.meta["fdm"] == pytest.approx(fdm)
    assert book.weights["X"] == pytest.approx(0.1 * 0.5 * fdm * 10 / 10 / 0.2)
    assert book.weights["Y"] == pytest.approx(0.1 * 0.5 * fdm * 5 / 10 / 0.4)


def test_vol_target_combined_forecast_is_capped_at_20() -> None:
    signals = {"a": {"X": 20.0, "Y": 1.0}, "b": {"X": 20.0, "Y": 1.0}}
    book = get_constructor("vol_target", tau=0.05, idm=1.0).target_weights(
        _inp(signals, vols=VOLS, forecast_history=_uncorrelated())
    )
    # FDM*20 = 28 caps at 20
    assert book.weights["X"] == pytest.approx(0.05 * 0.5 * 20 / 10 / 0.2)


def test_fdm_floors_negative_correlation_and_needs_20_observations() -> None:
    a = np.tile([1.0, -1.0], 16)
    hist = pd.DataFrame({"a": a, "b": -a})
    assert diversification_multiplier(hist, {"a": 0.5, "b": 0.5}) == pytest.approx(math.sqrt(2))
    assert diversification_multiplier(hist.iloc[:19], {"a": 0.5, "b": 0.5}) == 1.0
    assert diversification_multiplier(None, {"a": 0.5, "b": 0.5}) == 1.0
    assert diversification_multiplier(hist, {"a": 0.5, "c": 0.5}) == 1.0


def test_diversification_multiplier_caps_at_2_5() -> None:
    rng = np.random.default_rng(0)
    hist = pd.DataFrame(rng.normal(size=(500, 20)), columns=[f"T{i}" for i in range(20)])
    weights = dict.fromkeys(hist.columns, 1 / 20)
    assert diversification_multiplier(hist, weights) == 2.5


def test_vol_target_auto_idm_from_returns() -> None:
    returns = _uncorrelated().rename(columns={"a": "X", "b": "Y"})
    book = get_constructor("vol_target", tau=0.1, idm="auto").target_weights(
        _inp({"s": {"X": 10.0, "Y": 20.0}}, vols=VOLS, returns_history=returns)
    )
    assert book.meta["idm"] == pytest.approx(math.sqrt(2))
    assert book.weights["X"] == pytest.approx(0.25 * math.sqrt(2))


def test_vol_target_caps_gross_at_max_gross() -> None:
    book = get_constructor("vol_target", tau=0.4, idm=2.5).target_weights(
        _inp({"s": {"X": 20.0, "Y": 20.0}}, vols={"X": 0.01, "Y": 0.02})
    )
    assert book.gross == pytest.approx(1.0)
    assert book.weights["X"] == pytest.approx(2 * book.weights["Y"])


def test_vol_target_settings_bounds() -> None:
    with pytest.raises(ValueError):
        get_constructor("vol_target", tau=0.5)
    with pytest.raises(ValueError):
        get_constructor("vol_target", idm=3.0)
    with pytest.raises(ValueError):
        get_constructor("vol_target", max_gross=1.5)


def test_vol_target_uses_forecast_signals() -> None:
    assert get_constructor("vol_target").signal_method == "forecast"


# --- properties over every registered constructor -----------------------------


def _random_input(rng: np.random.Generator) -> ConstructionInput:
    tickers = [f"T{i}" for i in range(rng.integers(1, 30))]
    signals = {
        f"s{j}": {t: float(rng.normal(scale=15)) for t in tickers if rng.random() < 0.7}
        for j in range(rng.integers(1, 4))
    }
    vols = {t: float(rng.uniform(0.005, 1.0)) for t in tickers if rng.random() < 0.9}
    prices = {t: float(rng.uniform(1, 500)) for t in tickers if rng.random() < 0.9}
    return ConstructionInput(
        signals=signals,
        portfolio=Portfolio(cash=1e5),
        prices=prices,
        vols_annual=vols,
        as_of=date(2026, 1, 2),
    )


@pytest.mark.parametrize("name", constructor_names())
def test_every_constructor_is_long_only_capped_and_tradable(name: str) -> None:
    rng = np.random.default_rng(42)
    constructor = get_constructor(name)
    for _ in range(200):
        inp = _random_input(rng)
        book = constructor.target_weights(inp)
        assert book.gross <= 1.0 + 1e-9
        assert all(w > 0 for w in book.weights.values())
        assert all(inp.tradable(t) for t in book.weights)
        for t in book.weights:
            assert sum(book.attribution[t].values()) == pytest.approx(1.0)


# --- RS-06: all-positive picks keep every name ------------------------------------


def _piped(name: str, raw: dict[str, dict[str, float]], **settings):
    """Normalise ``raw`` the way the pipeline does, then construct."""
    from stonks.portfolio.signals import normalize

    constructor = get_constructor(name, **settings)
    vols = {t: 0.2 for s in raw.values() for t in s}
    signals = normalize(raw, constructor.signal_method, long_only=True)
    return constructor.target_weights(_inp(signals, vols=vols))


@pytest.mark.parametrize("name", ["equal_weight_top_n", "inverse_vol"])
def test_ten_rising_positive_picks_fill_ten_slots(name) -> None:
    raw = {"s": {f"T{i}": 0.01 * (i + 1) for i in range(10)}}
    key = "n" if name == "equal_weight_top_n" else "top_n"
    book = _piped(name, raw, **{key: 10})
    assert len(book.weights) == 10
    assert sum(book.weights.values()) == pytest.approx(1.0)


@pytest.mark.parametrize("name", ["equal_weight_top_n", "inverse_vol"])
def test_three_equal_scores_get_three_equal_weights(name) -> None:
    book = _piped(name, {"s": {"A": 0.5, "B": 0.5, "C": 0.5}})
    assert book.weights == pytest.approx({"A": 1 / 3, "B": 1 / 3, "C": 1 / 3})


def test_a_constant_cross_section_under_zscore_is_equal_conviction() -> None:
    from stonks.portfolio.signals import normalize

    out = normalize({"s": {"A": 0.5, "B": 0.5, "C": 0.5, "D": 0.5}}, "zscore")
    assert out["s"] == pytest.approx(dict.fromkeys("ABCD", 1.0))


def test_vol_target_ignores_names_with_a_clipped_zero_forecast() -> None:
    """RS-22: iw = 1/N counts only names with a positive forecast."""
    c = get_constructor("vol_target", tau=0.2, idm=1.0)
    book = c.target_weights(_inp({"s": {"A": 10.0, "B": 0.0}}, vols={"A": 0.2, "B": 0.2}))
    alone = c.target_weights(_inp({"s": {"A": 10.0}}, vols={"A": 0.2}))
    assert book.weights.get("B", 0.0) == 0.0
    assert book.weights["A"] == pytest.approx(alone.weights["A"])

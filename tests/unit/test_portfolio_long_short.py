"""Long/short construction (roadmap 16.3): signed targets with gross and
net limits, dollar and beta neutrality. Every case is checked by hand."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError

from stonks.core.types import Portfolio
from stonks.portfolio.base import (
    ConstructionInput,
    ConstructorSettings,
    constructor_names,
    estimate_betas,
    get_constructor,
)

SIGNALS = {"s": {"A": 2.0, "B": 1.0, "C": 0.5, "D": -0.5, "E": -1.0, "F": -2.0}}


def _inp(signals=SIGNALS, **kw) -> ConstructionInput:
    tickers = {t for s in signals.values() for t in s}
    return ConstructionInput(
        signals=signals,
        portfolio=Portfolio(cash=1000.0),
        prices=dict.fromkeys(tickers, 10.0),
        as_of=date(2026, 1, 2),
        **kw,
    )


def _ew(**settings):
    return get_constructor("equal_weight_top_n", long_only=False, **settings)


# --- settings -------------------------------------------------------------------


def test_cash_book_may_not_exceed_one_gross():
    with pytest.raises(ValidationError, match=r"1.0 gross"):
        ConstructorSettings(max_gross=1.5)


def test_long_short_book_may_run_above_one_gross():
    assert ConstructorSettings(long_only=False, max_gross=2.0).max_gross == 2.0


def test_net_limits_must_be_ordered():
    with pytest.raises(ValidationError, match="min_net"):
        ConstructorSettings(long_only=False, min_net=0.5, max_net=0.0)


def test_long_only_defaults_are_unchanged():
    s = ConstructorSettings()
    assert (s.long_only, s.max_gross, s.neutral) == (True, 1.0, "none")


# --- equal weight long/short ------------------------------------------------------


def test_equal_weight_long_short_hand_checked():
    # 2 longs and 2 shorts share a gross of 2.0: 0.5 each.
    book = _ew(n=2, max_gross=2.0).target_weights(_inp())
    assert book.weights == pytest.approx({"A": 0.5, "B": 0.5, "E": -0.5, "F": -0.5})
    assert book.gross == pytest.approx(2.0)
    assert book.net == pytest.approx(0.0)


def test_equal_weight_long_short_uneven_legs():
    # 3 longs, 1 short: four names at 0.5, gross 2.0, net 1.0.
    book = _ew(n=3, n_short=1, max_gross=2.0).target_weights(_inp())
    assert book.weights == pytest.approx({"A": 0.5, "B": 0.5, "C": 0.5, "F": -0.5})
    assert book.gross == pytest.approx(2.0)
    assert book.net == pytest.approx(1.0)


def test_max_net_scales_the_long_leg_down():
    # longs 1.5, shorts 0.5; net must be <= 0.5, so longs go to 1.0.
    book = _ew(n=3, n_short=1, max_gross=2.0, max_net=0.5).target_weights(_inp())
    assert book.weights["A"] == pytest.approx(1.0 / 3.0)
    assert book.weights["F"] == pytest.approx(-0.5)
    assert book.net == pytest.approx(0.5)
    assert book.gross == pytest.approx(1.5)


def test_min_net_scales_the_short_leg_down():
    # 1 long, 3 shorts at 0.5: net -1.0; min_net -0.5 shrinks shorts to 1.0.
    book = _ew(n=1, n_short=3, max_gross=2.0, min_net=-0.5).target_weights(_inp())
    assert book.weights["A"] == pytest.approx(0.5)
    assert book.weights["D"] == pytest.approx(-1.0 / 3.0)
    assert book.net == pytest.approx(-0.5)


def test_dollar_neutral_matches_the_legs():
    # longs 1.5 vs shorts 0.5: the long leg shrinks to 0.5.
    book = _ew(n=3, n_short=1, max_gross=2.0, neutral="dollar").target_weights(_inp())
    assert book.weights == pytest.approx({"A": 1 / 6, "B": 1 / 6, "C": 1 / 6, "F": -0.5})
    assert book.net == pytest.approx(0.0)
    assert book.gross == pytest.approx(1.0)


def test_beta_neutral_uses_given_betas():
    # A long at beta 2.0 (0.5 * 2 = 1.0), F short at beta 0.5 (0.5 * 0.5 =
    # 0.25): the long leg scales by 0.25, so beta exposure nets to 0.
    book = _ew(n=1, n_short=1, max_gross=1.0, neutral="beta").target_weights(
        _inp(betas={"A": 2.0, "F": 0.5})
    )
    assert book.weights == pytest.approx({"A": 0.125, "F": -0.5})
    assert 2.0 * book.weights["A"] + 0.5 * book.weights["F"] == pytest.approx(0.0)


def test_long_only_equal_weight_ignores_negatives():
    book = get_constructor("equal_weight_top_n", n=2).target_weights(_inp())
    assert book.weights == pytest.approx({"A": 0.5, "B": 0.5})


def test_equal_weight_long_short_normalises_with_signed_ranks():
    assert _ew().normalization() == "signed_rank"
    assert get_constructor("equal_weight_top_n").normalization() == "rank"


# --- vol target long/short --------------------------------------------------------


def test_vol_target_long_short_keeps_short_forecasts():
    # w = tau * idm * iw * F / 10 / sigma = 0.2 * 1 * 0.5 * F / 10 / 0.2
    book = get_constructor("vol_target", long_only=False, idm=1.0, max_gross=2.0).target_weights(
        _inp({"s": {"X": 10.0, "Y": -20.0}}, vols_annual={"X": 0.2, "Y": 0.2})
    )
    assert book.weights == pytest.approx({"X": 0.5, "Y": -1.0})
    assert book.gross == pytest.approx(1.5)


def test_vol_target_dollar_neutral():
    book = get_constructor("vol_target", long_only=False, idm=1.0, neutral="dollar").target_weights(
        _inp({"s": {"X": 10.0, "Y": -20.0}}, vols_annual={"X": 0.2, "Y": 0.2})
    )
    # shorts 1.0 vs longs 0.5: the short leg shrinks to 0.5
    assert book.weights == pytest.approx({"X": 0.5, "Y": -0.5})


def test_vol_target_long_only_clips_shorts():
    book = get_constructor("vol_target", idm=1.0).target_weights(
        _inp({"s": {"X": 10.0, "Y": -20.0}}, vols_annual={"X": 0.2, "Y": 0.2})
    )
    assert book.weights == pytest.approx({"X": 0.5})


def test_beta_neutral_needs_a_beta_aware_constructor():
    with pytest.raises(ValueError, match="beta"):
        get_constructor("inverse_vol", long_only=False, neutral="beta")


# --- betas ------------------------------------------------------------------------


def test_estimate_betas_against_the_equal_weight_market():
    rng = np.random.default_rng(0)
    market = rng.normal(0, 0.01, 300)
    history = pd.DataFrame({"HI": 1.5 * market, "LO": 0.5 * market})
    betas = estimate_betas(history)
    # the market proxy is the mean of the two, i.e. 1.0 * market
    assert betas["HI"] == pytest.approx(1.5)
    assert betas["LO"] == pytest.approx(0.5)


def test_estimate_betas_needs_enough_history():
    assert estimate_betas(pd.DataFrame({"A": [0.01, 0.02]})) == {}
    assert estimate_betas(None) == {}


# --- properties over every registered constructor ---------------------------------


@pytest.mark.parametrize("name", [n for n in constructor_names() if n != "single_winner"])
def test_long_short_books_respect_gross_and_net(name):
    try:
        constructor = get_constructor(
            name, long_only=False, max_gross=1.5, min_net=-0.2, max_net=0.3
        )
    except (ValidationError, TypeError):
        pytest.skip(f"{name} has no long/short settings")
    vols = {t: 0.2 + 0.05 * i for i, t in enumerate("ABCDEF")}
    rng = np.random.default_rng(1)
    returns = pd.DataFrame(rng.normal(0, 0.01, (80, 6)), columns=list("ABCDEF"))
    book = constructor.target_weights(_inp(vols_annual=vols, returns_history=returns))
    assert book.gross <= 1.5 + 1e-9
    assert -0.2 - 1e-9 <= book.net <= 0.3 + 1e-9

"""HRP and ERC: analytic cases, then the registered constructors (BL-44)."""

from __future__ import annotations

import math
from datetime import date

import numpy as np
import pandas as pd
import pytest

from stonks.core.types import Portfolio
from stonks.portfolio.base import ConstructionInput, constructor_names, get_constructor
from stonks.portfolio.diversification import risk_contributions
from stonks.portfolio.erc import erc_weights
from stonks.portfolio.hrp import hrp_weights

AS_OF = date(2026, 1, 30)


def _block_cov() -> np.ndarray:
    """Two blocks: A (vol 0.1, rho 0.5) and B (vol 0.2, rho 0.5), uncorrelated."""
    a = np.array([[0.01, 0.005], [0.005, 0.01]])
    b = np.array([[0.04, 0.02], [0.02, 0.04]])
    cov = np.zeros((4, 4))
    cov[:2, :2] = a
    cov[2:, 2:] = b
    return cov


# --- HRP ------------------------------------------------------------------------


def test_hrp_on_block_diagonal_matches_hand_calculation() -> None:
    w = hrp_weights(_block_cov())
    var_a = 0.25 * (0.01 + 0.01 + 2 * 0.005)
    var_b = 0.25 * (0.04 + 0.04 + 2 * 0.02)
    alpha_a = 1 - var_a / (var_a + var_b)
    np.testing.assert_allclose(w, [alpha_a / 2] * 2 + [(1 - alpha_a) / 2] * 2)
    assert w.sum() == pytest.approx(1.0)


def test_hrp_on_diagonal_is_inverse_variance() -> None:
    vols = np.array([0.1, 0.2, 0.3, 0.15, 0.25])
    w = hrp_weights(np.diag(vols**2))
    expected = (1 / vols**2) / (1 / vols**2).sum()
    np.testing.assert_allclose(w, expected)


def test_hrp_single_asset_and_duplicates() -> None:
    assert hrp_weights(np.array([[0.04]])).tolist() == [1.0]
    cov = np.array([[0.04, 0.04, 0.0], [0.04, 0.04, 0.0], [0.0, 0.0, 0.04]])
    w = hrp_weights(cov)
    assert w.sum() == pytest.approx(1.0)
    assert w[0] == pytest.approx(w[1])
    assert np.all(w > 0)


def test_hrp_rejects_unknown_linkage() -> None:
    with pytest.raises(ValueError):
        hrp_weights(_block_cov(), linkage_method="nope")


# --- ERC ------------------------------------------------------------------------


def test_erc_on_uncorrelated_assets_is_inverse_vol() -> None:
    vols = np.array([0.1, 0.2, 0.4])
    w = erc_weights(np.diag(vols**2))
    np.testing.assert_allclose(w, (1 / vols) / (1 / vols).sum(), rtol=1e-6)


def test_erc_risk_contributions_are_equal() -> None:
    rng = np.random.default_rng(1)
    a = rng.normal(size=(6, 6))
    cov = a @ a.T / 6 + np.eye(6) * 0.01
    w = erc_weights(cov)
    assert w.sum() == pytest.approx(1.0)
    np.testing.assert_allclose(risk_contributions(w, cov), np.full(6, 1 / 6), atol=1e-6)


def test_erc_budgets_set_the_risk_shares() -> None:
    cov = _block_cov()
    budgets = np.array([0.4, 0.3, 0.2, 0.1])
    w = erc_weights(cov, budgets)
    np.testing.assert_allclose(risk_contributions(w, cov), budgets, atol=1e-6)


def test_erc_is_scale_free_and_handles_singular() -> None:
    cov = _block_cov()
    np.testing.assert_allclose(erc_weights(cov), erc_weights(cov * 1e-6), rtol=1e-6)
    dup = np.array([[0.04, 0.04, 0.0], [0.04, 0.04, 0.0], [0.0, 0.0, 0.01]])
    w = erc_weights(dup)
    assert np.all(np.isfinite(w)) and w.sum() == pytest.approx(1.0)
    assert w[0] == pytest.approx(w[1], rel=1e-4)


def test_erc_rejects_bad_budgets() -> None:
    with pytest.raises(ValueError):
        erc_weights(np.eye(2), np.array([1.0, -1.0]))


# --- constructors ---------------------------------------------------------------


def _inp(scores, *, vols=None, history=None, positions=None, as_of=AS_OF) -> ConstructionInput:
    return ConstructionInput(
        signals={"s": scores},
        portfolio=Portfolio(cash=1000.0, positions=positions or {}),
        prices=dict.fromkeys(scores, 10.0),
        vols_annual=vols or {},
        returns_history=history,
        as_of=as_of,
    )


def _history(n: int = 300, seed: int = 0, end: date = AS_OF) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    factor = rng.normal(scale=0.01, size=(n, 1))
    data = np.hstack(
        [
            factor + rng.normal(scale=0.005, size=(n, 2)),  # A and B move together
            rng.normal(scale=0.01, size=(n, 1)),  # C alone
        ]
    )
    index = pd.bdate_range(end=end, periods=n)
    return pd.DataFrame(data, index=index, columns=["A", "B", "C"])


def test_constructors_are_registered() -> None:
    assert {"hrp", "erc", "mean_variance_costs"} <= set(constructor_names())


def test_erc_constructor_without_history_matches_inverse_vol() -> None:
    scores = {"A": 1.0, "B": 0.5, "C": 2.0}
    vols = {"A": 0.1, "B": 0.2, "C": 0.4}
    erc = get_constructor("erc").target_weights(_inp(scores, vols=vols)).weights
    iv = get_constructor("inverse_vol").target_weights(_inp(scores, vols=vols)).weights
    assert erc == pytest.approx(iv, rel=1e-6)


def test_hrp_constructor_without_history_is_inverse_variance() -> None:
    vols = {"A": 0.1, "B": 0.2}
    book = get_constructor("hrp").target_weights(_inp({"A": 1.0, "B": 1.0}, vols=vols))
    assert book.weights == pytest.approx({"A": 0.8, "B": 0.2})
    assert book.meta["covariance"] == "vols"
    # variance shares 0.8 and 0.2
    assert book.meta["enb"] == pytest.approx(math.exp(-(0.8 * math.log(0.8) + 0.2 * math.log(0.2))))


@pytest.mark.parametrize("name", ["hrp", "erc"])
def test_history_is_used_and_correlated_names_share_a_bet(name: str) -> None:
    book = get_constructor(name).target_weights(
        _inp({"A": 1.0, "B": 1.0, "C": 1.0}, history=_history())
    )
    assert book.meta["covariance"] == "history"
    assert sum(book.weights.values()) == pytest.approx(1.0)
    # C is its own bet, A and B are close to one: C gets the most
    assert book.weights["C"] > book.weights["A"]
    assert book.weights["C"] > book.weights["B"]
    assert 1.0 < book.meta["enb"] < 3.0


@pytest.mark.parametrize("name", ["hrp", "erc", "mean_variance_costs"])
def test_rows_after_as_of_are_ignored(name: str) -> None:
    past = _history(end=AS_OF)
    future = _history(n=50, seed=9, end=date(2026, 4, 30)) * 20
    future = future[future.index > pd.Timestamp(AS_OF)]
    scores = {"A": 1.0, "B": 1.0, "C": 1.0}
    c = get_constructor(name)
    clean = c.target_weights(_inp(scores, history=past)).weights
    leaky = c.target_weights(_inp(scores, history=pd.concat([past, future]))).weights
    assert leaky == pytest.approx(clean)


@pytest.mark.parametrize("name", ["hrp", "erc"])
def test_short_history_falls_back_to_vols(name: str) -> None:
    short = _history(n=10)
    book = get_constructor(name, min_observations=60).target_weights(
        _inp({"A": 1.0, "B": 1.0}, vols={"A": 0.1, "B": 0.1}, history=short)
    )
    assert book.meta["covariance"] == "vols"
    assert book.weights == pytest.approx({"A": 0.5, "B": 0.5})


@pytest.mark.parametrize("name", ["hrp", "erc", "mean_variance_costs"])
def test_names_without_history_or_vol_are_dropped(name: str) -> None:
    book = get_constructor(name).target_weights(_inp({"A": 1.0, "Z": 1.0}, vols={"A": 0.2}))
    assert set(book.weights) <= {"A"}
    assert get_constructor(name).target_weights(_inp({"Z": 1.0})).weights == {}


@pytest.mark.parametrize("name", ["hrp", "erc"])
def test_max_weight_caps_and_redistributes(name: str) -> None:
    vols = {"A": 0.05, "B": 0.3, "C": 0.3}
    book = get_constructor(name, max_weight=0.4).target_weights(
        _inp({"A": 1.0, "B": 1.0, "C": 1.0}, vols=vols)
    )
    assert max(book.weights.values()) <= 0.4 + 1e-9
    assert sum(book.weights.values()) == pytest.approx(1.0)
    # too few names to fill the book at the cap: the rest is cash
    tiny = get_constructor(name, max_weight=0.3).target_weights(
        _inp({"A": 1.0, "B": 1.0}, vols=vols)
    )
    assert tiny.weights == pytest.approx({"A": 0.3, "B": 0.3})


@pytest.mark.parametrize("name", ["hrp", "erc"])
def test_deterministic_regardless_of_signal_order(name: str) -> None:
    hist = _history()
    one = get_constructor(name).target_weights(_inp({"A": 1.0, "B": 2.0, "C": 3.0}, history=hist))
    two = get_constructor(name).target_weights(_inp({"C": 3.0, "B": 2.0, "A": 1.0}, history=hist))
    assert one.weights == two.weights


def test_top_n_keeps_the_best_scores() -> None:
    vols = {"A": 0.2, "B": 0.2, "C": 0.2}
    book = get_constructor("erc", top_n=2).target_weights(
        _inp({"A": 3.0, "B": 2.0, "C": 1.0}, vols=vols)
    )
    assert set(book.weights) == {"A", "B"}


def test_erc_score_budget_gives_more_risk_to_better_scores() -> None:
    vols = {"A": 0.2, "B": 0.2}
    book = get_constructor("erc", budget="score").target_weights(
        _inp({"A": 3.0, "B": 1.0}, vols=vols)
    )
    # risk shares 3:1 on equal uncorrelated vols means weights sqrt(3):1
    a = 3**0.5 / (3**0.5 + 1)
    assert book.weights == pytest.approx({"A": a, "B": 1 - a}, rel=1e-5)


def test_unknown_estimator_fails_at_build() -> None:
    with pytest.raises(ValueError, match="estimator"):
        get_constructor("hrp", estimator="nope")


@pytest.mark.parametrize("method", ["hrp", "erc", "mean_variance_costs"])
def test_construction_settings_build_the_new_methods(method: str) -> None:
    from stonks.portfolio.settings import ConstructionSettings

    settings = ConstructionSettings.from_mapping(
        {"method": method, "estimator": "ewma", "max_weight": 0.25, "top_n": 8}
    )
    constructor = settings.build()
    assert constructor.name == method
    assert constructor.settings.max_weight == 0.25

"""The construction pipeline shared by the tick and the backtest (BL-12)."""

from __future__ import annotations

from datetime import date

import pytest

from stonks.config import RiskPolicy
from stonks.core.types import Order, Portfolio
from stonks.portfolio.pipeline import (
    PORTFOLIO_STRATEGY,
    BookInput,
    ConstructionSettings,
    MarketView,
    build_orders,
    vols_from_history,
)

AS_OF = date(2026, 3, 20)
PRICES = {"X": 10.0, "Y": 20.0, "Z": 50.0, "H": 5.0}


class Recorder:
    """A strategy whose decide records its picks and buys each pick once."""

    def __init__(self, sid: str) -> None:
        self.id = sid
        self.calls: list[list[tuple[float, str]]] = []

    def decide(self, my_picks, portfolio, prices, as_of):
        self.calls.append(list(my_picks))
        orders = [
            Order(client_id=f"raw:{t}", ticker=t, side="sell", quantity=q)
            for t, q in portfolio.positions.items()
            if q > 0 and t not in {p for _, p in my_picks}
        ]
        orders += [
            Order(client_id=f"raw:{t}", ticker=t, side="buy", quantity=10.0) for _, t in my_picks
        ]
        return orders


def _market(**kw) -> MarketView:
    return MarketView(as_of=AS_OF, prices=PRICES, **kw)


def _strategies(*ids: str) -> dict[str, Recorder]:
    return {sid: Recorder(sid) for sid in ids}


# ---- settings ------------------------------------------------------------------


def test_construction_settings_split_pipeline_and_constructor_knobs():
    s = ConstructionSettings.from_mapping(
        {"method": "equal_weight_top_n", "n": 3, "max_gross": 0.8, "buffer_fraction": 0.2}
    )
    assert s.method == "equal_weight_top_n"
    assert s.buffer_fraction == 0.2
    assert s.params == {"n": 3, "max_gross": 0.8}
    assert s.build().settings.n == 3


def test_construction_settings_default_to_single_winner_and_reject_unknown():
    assert ConstructionSettings().method == "single_winner"
    with pytest.raises(ValueError, match="unknown portfolio constructor"):
        ConstructionSettings(method="nope")
    with pytest.raises(ValueError):
        ConstructionSettings(method="equal_weight_top_n", params={"bogus": 1})


# ---- single winner: the winner's own decide ---------------------------------------


def test_single_winner_hands_the_book_to_the_best_pick_via_decide():
    strategies = _strategies("a", "b")
    result = build_orders(
        {"a": {"X": 0.3, "Y": 0.1}, "b": {"Z": 0.5}},
        BookInput(portfolio=Portfolio(cash=1_000.0)),
        _market(),
        strategies=strategies.__getitem__,
    )
    assert result.decided_by == "b"
    assert result.winner_return == 0.5
    assert strategies["b"].calls == [[(0.5, "Z")]]
    assert strategies["a"].calls == []
    [order] = result.orders
    assert (order.ticker, order.side, order.strategy_id) == ("Z", "buy", "b")
    assert order.client_id == "2026-03-20:b:Z:buy"
    assert result.attribution == {"Z": {"b": 1.0}}


def test_single_winner_keeps_threshold_filtering_to_the_signal_phase():
    """Scores at or below 0 that the ranker let through still count."""
    strategies = _strategies("a")
    result = build_orders(
        {"a": {"X": -0.2}},
        BookInput(portfolio=Portfolio(cash=1_000.0)),
        _market(),
        strategies=strategies.__getitem__,
    )
    assert strategies["a"].calls == [[(-0.2, "X")]]
    assert result.winner_return == -0.2


def test_single_winner_only_sees_the_books_strategies():
    strategies = _strategies("a", "b")
    result = build_orders(
        {"a": {"X": 0.3}, "b": {"Z": 0.5}},
        BookInput(portfolio=Portfolio(cash=1_000.0), strategy_weights={"a": 1.0, "b": 0.0}),
        _market(),
        strategies=strategies.__getitem__,
    )
    assert result.decided_by == "a"


def test_nothing_ranked_and_flat_is_no_candidates():
    strategies = _strategies("a")
    result = build_orders(
        {"a": {}},
        BookInput(portfolio=Portfolio(cash=1_000.0)),
        _market(),
        strategies=strategies.__getitem__,
        exit_owner=lambda: pytest.fail("no owner lookup for a flat book"),
    )
    assert result.reason == "no_candidates"
    assert result.orders == [] and strategies["a"].calls == []


def test_nothing_ranked_with_holdings_lets_the_owner_exit():
    strategies = _strategies("a")
    result = build_orders(
        {"a": {}},
        BookInput(portfolio=Portfolio(cash=0.0, positions={"H": 4.0})),
        _market(),
        strategies=strategies.__getitem__,
        exit_owner=lambda: "a",
    )
    assert result.exit_only and result.decided_by == "a" and result.reason is None
    assert strategies["a"].calls == [[]]
    assert [(o.ticker, o.side, o.quantity) for o in result.orders] == [("H", "sell", 4.0)]


def test_nothing_ranked_without_an_owner_is_no_active_owner():
    result = build_orders(
        {},
        BookInput(portfolio=Portfolio(cash=0.0, positions={"H": 4.0})),
        _market(),
        strategies=_strategies().__getitem__,
        exit_owner=lambda: None,
    )
    assert result.reason == "no_active_owner" and result.orders == []


def test_stale_buys_are_dropped_and_risk_clips():
    strategies = _strategies("a")
    result = build_orders(
        {"a": {"X": 0.4, "Y": 0.3}},
        BookInput(
            portfolio=Portfolio(cash=1_000.0),
            risk=RiskPolicy(max_weight_per_ticker=0.05),
        ),
        _market(buyable={"X"}),
        strategies=strategies.__getitem__,
    )
    assert result.stale_buys == ["Y"]
    [order] = result.orders
    assert order.ticker == "X" and order.quantity == pytest.approx(5.0)
    assert [a.rule for a in result.adjustments] == ["max_weight_per_ticker"]


def test_a_strategy_slice_policy_tightens_the_winner():
    strategies = _strategies("a")
    result = build_orders(
        {"a": {"X": 0.4}},
        BookInput(
            portfolio=Portfolio(cash=1_000.0),
            risk=RiskPolicy(),
            risk_overrides={"a": RiskPolicy(max_weight_per_ticker=0.02)},
        ),
        _market(),
        strategies=strategies.__getitem__,
    )
    assert result.orders[0].quantity == pytest.approx(2.0)


# ---- combined constructors ------------------------------------------------------


def _equal(n: int = 4) -> ConstructionSettings:
    return ConstructionSettings(
        method="equal_weight_top_n", params={"n": n}, buffer_fraction=0.0, min_trade_weight=0.0
    )


def test_equal_weight_gives_every_strategy_capital_and_attributes_it():
    result = build_orders(
        {"a": {"X": 0.3, "Y": 0.1, "H": 0.2}, "b": {"Z": 0.5, "X": 0.4, "Y": 0.2}},
        BookInput(portfolio=Portfolio(cash=1_000.0), construction=_equal(2)),
        _market(),
    )
    assert set(result.target_book.weights) == {"X", "Z"}
    for ticker, shares in result.attribution.items():
        assert sum(shares.values()) == pytest.approx(1.0), ticker
    assert set(result.attribution["X"]) == {"a", "b"}
    by_ticker = {o.ticker: o for o in result.orders}
    assert by_ticker["Z"].strategy_id == "b"
    assert by_ticker["Z"].client_id == "2026-03-20:b:Z:buy"
    assert {o.side for o in result.orders} == {"buy"}
    assert result.decided_by is None


def test_combined_mode_exits_untargeted_holdings_without_an_owner_lookup():
    result = build_orders(
        {"a": {"X": 0.3}},
        BookInput(
            portfolio=Portfolio(cash=100.0, positions={"H": 4.0}),
            construction=_equal(),
            prior_attribution={"H": {"b": 0.7, "a": 0.3}},
        ),
        _market(),
        exit_owner=lambda: pytest.fail("combined mode needs no owner"),
    )
    sell = next(o for o in result.orders if o.side == "sell")
    assert (sell.ticker, sell.quantity, sell.strategy_id) == ("H", 4.0, "b")


def test_combined_exit_without_prior_attribution_belongs_to_the_portfolio():
    result = build_orders(
        {"a": {"X": 0.3}},
        BookInput(portfolio=Portfolio(cash=100.0, positions={"H": 4.0}), construction=_equal()),
        _market(),
    )
    sell = next(o for o in result.orders if o.side == "sell")
    assert sell.strategy_id is None
    assert sell.client_id == f"2026-03-20:{PORTFOLIO_STRATEGY}:H:sell"


def test_combined_mode_holds_when_no_book_strategy_produced_signals():
    """A signal phase that failed must not liquidate the book."""
    result = build_orders(
        {"other": {"X": 1.0}},
        BookInput(
            portfolio=Portfolio(cash=0.0, positions={"H": 4.0}),
            construction=_equal(),
            strategy_weights={"a": 1.0},
        ),
        _market(),
    )
    assert result.orders == [] and result.reason == "no_signals"


def test_client_id_callback_scopes_ids():
    result = build_orders(
        {"a": {"X": 0.3}},
        BookInput(portfolio=Portfolio(cash=1_000.0), construction=_equal()),
        _market(),
        client_id=lambda sid, ticker, side: f"pf_1:{sid}:{ticker}:{side}",
    )
    assert [o.client_id for o in result.orders] == ["pf_1:a:X:buy"]


def test_vols_from_history_annualises_recent_returns():
    import numpy as np
    import pandas as pd

    idx = pd.bdate_range("2026-01-01", periods=80)
    rng = np.random.default_rng(0)
    close = pd.Series(100 * np.exp(np.cumsum(rng.normal(0, 0.01, 80))), index=idx)
    vols = vols_from_history(
        {"X": pd.DataFrame({"close": close}), "Y": close.to_frame("close")[:3]}
    )
    assert set(vols) == {"X"}
    assert 0.05 < vols["X"] < 0.4

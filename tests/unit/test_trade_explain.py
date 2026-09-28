"""Why did or didn't we trade (roadmap 23.7): the step that kept a ticker
out of the book or trimmed it, derived from one pipeline run."""

from __future__ import annotations

from datetime import date

from stonks.config import RiskPolicy
from stonks.core.types import Order, Portfolio
from stonks.portfolio.explain import TickerDecision, explain, mark
from stonks.portfolio.pipeline import BookInput, ConstructionSettings, MarketView, build_orders

AS_OF = date(2026, 3, 20)
PRICES = {"X": 10.0, "Y": 20.0, "Z": 50.0, "H": 5.0}


class Buyer:
    def __init__(self, sid: str) -> None:
        self.id = sid

    def decide(self, my_picks, portfolio, prices, as_of):
        return [
            Order(client_id=f"raw:{t}", ticker=t, side="buy", quantity=10.0)
            for _, t in my_picks
            if portfolio.positions.get(t, 0.0) == 0.0
        ]


def _by_ticker(decisions: list[TickerDecision]) -> dict[str, TickerDecision]:
    return {d.ticker: d for d in decisions}


def _run(raw, book, market=None, universe=None):
    market = market or MarketView(as_of=AS_OF, prices=PRICES)
    kept = {
        sid: {t: s for t, s in scores.items() if universe is None or t in universe}
        for sid, scores in raw.items()
    }
    result = build_orders(
        kept, book, market, strategies=lambda sid: Buyer(sid), exit_owner=lambda: None
    )
    return result, _by_ticker(
        explain(
            raw,
            kept,
            result,
            book.portfolio,
            market.prices,
            universe=universe,
            constructor=book.construction.method,
        )
    )


def test_single_winner_traded_and_rank_cut():
    result, why = _run(
        {"a": {"X": 0.3, "Y": 0.1}, "b": {"Z": 0.5}},
        BookInput(portfolio=Portfolio(cash=1_000.0)),
    )
    assert why["Z"].step == "traded" and why["Z"].outcome == "traded"
    assert why["Z"].strategy_id == "b"
    assert why["X"].step == "rank" and why["X"].outcome == "kept_out"
    assert why["X"].detail["winner"] == "b"
    assert why["X"].strategies == ("a",)
    assert why["Y"].step == "rank"


def test_outside_the_universe():
    _, why = _run(
        {"a": {"X": 0.3, "Z": 0.9}},
        BookInput(portfolio=Portfolio(cash=1_000.0)),
        universe={"X"},
    )
    assert why["Z"].step == "universe" and why["Z"].outcome == "kept_out"
    assert why["X"].step == "traded"


def test_constructor_weight_zero_on_the_target_route():
    book = BookInput(
        portfolio=Portfolio(cash=1_000.0),
        construction=ConstructionSettings.from_mapping({"method": "equal_weight_top_n", "n": 1}),
    )
    result, why = _run({"a": {"X": 0.3, "Z": 0.9}}, book)
    [picked] = result.target_book.weights
    [dropped] = {"X", "Z"} - {picked}
    assert why[picked].step == "traded"
    assert why[dropped].step == "constructor" and why[dropped].outcome == "kept_out"
    assert why[dropped].detail["constructor"] == "equal_weight_top_n"


def test_buffer_keeps_a_near_target_holding():
    # X at 10 * 50 = 500 of 1000 equity: weight 0.5, target 0.5 (n=2 equal weight)
    book = BookInput(
        portfolio=Portfolio(cash=500.0, positions={"X": 50.0}),
        construction=ConstructionSettings.from_mapping({"method": "equal_weight_top_n", "n": 2}),
    )
    _, why = _run({"a": {"X": 0.3, "Z": 0.2}}, book)
    assert why["X"].step == "buffer" and why["X"].outcome == "held"
    assert why["X"].detail["target_weight"] == 0.5
    assert why["X"].detail["current_weight"] == 0.5


def test_stale_price_drops_the_buy():
    market = MarketView(as_of=AS_OF, prices=PRICES, buyable={"X"})
    _, why = _run(
        {"a": {"Z": 0.9}},
        BookInput(portfolio=Portfolio(cash=1_000.0)),
        market=market,
    )
    assert why["Z"].step == "stale_price" and why["Z"].outcome == "kept_out"


def test_named_risk_rule_trims_the_order():
    # 10 shares of Z at 50 = 500 of 1000: a 20% cap trims it to 4 shares
    book = BookInput(
        portfolio=Portfolio(cash=1_000.0),
        risk=RiskPolicy(max_weight_per_ticker=0.2),
    )
    _, why = _run({"a": {"Z": 0.9}}, book)
    assert why["Z"].step == "risk_rule" and why["Z"].outcome == "trimmed"
    assert why["Z"].detail["rule"] == "max_weight_per_ticker"
    assert why["Z"].detail["original_quantity"] == 10.0
    assert why["Z"].detail["adjusted_quantity"] < 10.0


def test_held_without_a_signal_is_held():
    book = BookInput(portfolio=Portfolio(cash=1_000.0, positions={"H": 3.0}))
    _, why = _run({"a": {"X": 0.3}}, book)
    assert why["H"].step == "held" and why["H"].outcome == "held"


def test_mark_overrides_later_steps():
    _, why = _run({"a": {"X": 0.3}}, BookInput(portfolio=Portfolio(cash=1_000.0)))
    marked = _by_ticker(mark(list(why.values()), ["X"], "halt", {"halt": "all"}))
    assert marked["X"].step == "halt" and marked["X"].outcome == "kept_out"
    assert marked["X"].detail["halt"] == "all"
    assert marked["X"].strategy_id == "a"


def test_as_dict_round_trip():
    d = TickerDecision(
        ticker="X",
        step="rank",
        outcome="kept_out",
        strategy_id="a",
        strategies=("a",),
        score=0.1,
        detail={"winner": "b"},
    )
    assert TickerDecision.from_dict(d.as_dict()) == d
    assert d.summary().startswith("X")

"""Phase 16.1: the risk layer and the construction pipeline in a book that
allows shorts. Long-only books keep today's behaviour."""

from __future__ import annotations

from datetime import date

import pytest

from stonks.config import RiskPolicy
from stonks.core.types import Order, Portfolio
from stonks.execution.orders import classify_all
from stonks.portfolio.pipeline import BookInput, ConstructionSettings, MarketView, build_orders
from stonks.production.risk import apply_risk
from stonks.production.rules import RiskBook, RiskContext, is_cover

AS_OF = date(2026, 3, 20)
PRICES = {"X": 10.0, "Y": 20.0}


def _o(cid: str, side: str, qty: float, ticker: str = "X", effect=None) -> Order:
    return Order(cid, ticker, side, qty, position_effect=effect)  # type: ignore[arg-type]


# ---- classify_all ------------------------------------------------------------------------


def test_classify_all_runs_against_the_running_position() -> None:
    legs = classify_all([_o("a:sell", "sell", 15.0), _o("b:sell", "sell", 5.0)], {"X": 10.0})
    assert [(o.client_id, o.position_effect, o.quantity) for o in legs] == [
        ("a:sell", "close", 10.0),
        ("a:short", "open", 5.0),
        ("b:short", "open", 5.0),
    ]
    # Already classified orders pass through, so it is safe to run twice.
    assert classify_all(legs, {"X": 10.0}) == legs


# ---- the caps ------------------------------------------------------------------------------


def test_a_short_sale_is_clipped_in_a_long_only_book() -> None:
    result = apply_risk([_o("s", "sell", 5.0)], Portfolio(cash=1_000.0), PRICES, {}, RiskPolicy())
    assert result.orders == []
    assert result.adjustments[0].rule == "sell_exceeds_position"


def test_a_short_sale_passes_the_caps_when_the_book_allows_shorts() -> None:
    result = apply_risk(
        [_o("s:sell", "sell", 15.0)],
        Portfolio(cash=1_000.0, positions={"X": 10.0}),
        PRICES,
        {},
        RiskPolicy(),
        allow_short=True,
    )
    assert [(o.position_effect, o.quantity) for o in result.orders] == [
        ("close", 10.0),
        ("open", 5.0),
    ]
    assert result.adjustments == []


def test_a_cover_skips_every_order_rule() -> None:
    # Weight cap 1 %, min notional 10,000 and a 90 % cash buffer would all
    # cut a buy. A cover of a short is position-reducing, so none apply.
    policy = RiskPolicy(
        max_weight_per_ticker=0.01, cash_buffer_fraction=0.9, min_order_notional=10_000.0
    )
    book = Portfolio(cash=1_200.0, positions={"X": -20.0})
    result = apply_risk([_o("c", "buy", 20.0)], book, PRICES, {}, policy)
    assert [(o.side, o.quantity) for o in result.orders] == [("buy", 20.0)]
    assert result.adjustments == []


def test_is_cover_and_risk_book_commit_on_shorts() -> None:
    assert is_cover(_o("c", "buy", 1.0), {"X": -2.0})
    assert not is_cover(_o("c", "buy", 1.0, effect="open"), {"X": -2.0})
    assert not is_cover(_o("s", "sell", 1.0), {"X": -2.0})
    ctx = RiskContext(
        portfolio=Portfolio(cash=100.0), prices=PRICES, asset_classes={}, policy=RiskPolicy()
    )
    book = RiskBook.open(ctx)
    book.commit(_o("s", "sell", 3.0), 3.0, ctx)
    assert book.positions == {"X": -3.0} and book.cash == pytest.approx(130.0)
    book.commit(_o("c", "buy", 3.0), 3.0, ctx)
    assert book.positions == {} and book.cash == pytest.approx(100.0)


# ---- the pipeline -------------------------------------------------------------------------


class _Shorter:
    supports_short = True

    def __init__(self, orders: list[Order] | None = None) -> None:
        self.orders = orders or []

    def decide(self, my_picks, portfolio, prices, as_of):
        return list(self.orders)


class _LongOnly(_Shorter):
    supports_short = False


def _vol_target_book(portfolio: Portfolio, allow_short: bool) -> BookInput:
    return BookInput(
        portfolio=portfolio,
        construction=ConstructionSettings(method="vol_target", params={"tau": 0.2}),
        allow_short=allow_short,
    )


def _market() -> MarketView:
    return MarketView(as_of=AS_OF, prices=PRICES, vols_annual={"X": 0.2, "Y": 0.2})


def _signals() -> dict[str, dict[str, float]]:
    return {"s": {"X": 2.0, "Y": -2.0}}


def test_target_route_shorts_only_when_book_and_strategy_both_allow() -> None:
    book = Portfolio(cash=1_000.0)
    both = build_orders(
        _signals(), _vol_target_book(book, True), _market(), strategies=lambda s: _Shorter()
    )
    assert both.target_book.weights["Y"] < 0 < both.target_book.weights["X"]
    short = [o for o in both.orders if o.ticker == "Y"]
    assert [(o.side, o.position_effect) for o in short] == [("sell", "open")]
    assert short[0].client_id.endswith(":Y:short")

    for allow, strategy in ((False, _Shorter()), (True, _LongOnly())):
        result = build_orders(
            _signals(),
            _vol_target_book(book, allow),
            _market(),
            strategies=lambda s, st=strategy: st,
        )
        assert "Y" not in result.target_book.weights
        assert all(o.side == "buy" for o in result.orders)


def test_single_winner_splits_at_zero_and_drops_unwanted_short_legs() -> None:
    held = Portfolio(cash=1_000.0, positions={"X": 10.0})
    sell = [Order("raw:X", "X", "sell", 15.0)]
    signals = {"s": {"X": 1.0}}
    market = MarketView(as_of=AS_OF, prices=PRICES)

    shorter = build_orders(
        signals,
        BookInput(portfolio=held, allow_short=True),
        market,
        strategies=lambda s: _Shorter(sell),
    )
    assert [(o.position_effect, o.quantity, o.client_id) for o in shorter.orders] == [
        ("close", 10.0, "2026-03-20:s:X:sell"),
        ("open", 5.0, "2026-03-20:s:X:short"),
    ]

    long_only = build_orders(
        signals,
        BookInput(portfolio=held, allow_short=True),
        market,
        strategies=lambda s: _LongOnly(sell),
    )
    assert [(o.position_effect, o.quantity) for o in long_only.orders] == [("close", 10.0)]


def test_be50_a_single_winner_short_is_attributed_to_its_strategy() -> None:
    held = Portfolio(cash=1_000.0, positions={"X": 10.0})
    sell = [Order("raw:X", "X", "sell", 15.0), Order("raw:Y", "Y", "sell", 5.0)]
    result = build_orders(
        {"s": {"X": 1.0}},
        BookInput(portfolio=held, allow_short=True),
        MarketView(as_of=AS_OF, prices=PRICES),
        strategies=lambda s: _Shorter(sell),
    )
    # the opening legs belong to the winner; the close of X is not a new share
    assert result.attribution == {"X": {"s": 1.0}, "Y": {"s": 1.0}}


def test_be50_single_winner_weights_follow_the_score_sign() -> None:
    from stonks.portfolio.base import ConstructionInput, get_constructor

    # a threshold below 0 ranks the negative pick too
    constructor = get_constructor("single_winner", long_only=False, max_gross=1.0, threshold=-1.0)
    book = constructor.target_weights(
        ConstructionInput(
            signals={"s": {"X": 0.5, "Y": -0.4}},
            portfolio=Portfolio(cash=1_000.0),
            prices=PRICES,
            as_of=AS_OF,
        )
    )
    assert book.weights["X"] > 0 > book.weights["Y"]

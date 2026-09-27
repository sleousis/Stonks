"""The construction pipeline runs a long/short constructor mode in a book
that may short, and a cash book otherwise (roadmap 16.3)."""

from __future__ import annotations

from datetime import date

import pytest

from stonks.core.types import Portfolio
from stonks.portfolio.pipeline import BookInput, ConstructionSettings, MarketView, build_orders

AS_OF = date(2026, 3, 20)
TICKERS = ["A", "B", "C", "D", "E", "F"]
PRICES = dict.fromkeys(TICKERS, 10.0)
SIGNALS = {"s": {"A": 3.0, "B": 2.0, "C": 1.0, "D": -1.0, "E": -2.0, "F": -3.0}}


class _Shorter:
    supports_short = True

    def decide(self, my_picks, portfolio, prices, as_of):
        return []


class _LongOnly(_Shorter):
    supports_short = False


def _book(allow_short: bool, **params) -> BookInput:
    return BookInput(
        portfolio=Portfolio(cash=10_000.0),
        construction=ConstructionSettings(
            method="equal_weight_top_n",
            params={"n": 2, "long_only": False, "max_gross": 2.0, **params},
            buffer_fraction=0.0,
        ),
        allow_short=allow_short,
    )


def test_market_neutral_book_hand_checked():
    # z-scores rank A, B long and E, F short; 2.0 gross over 4 names.
    result = build_orders(
        SIGNALS,
        _book(True, neutral="dollar"),
        MarketView(as_of=AS_OF, prices=PRICES),
        strategies=lambda s: _Shorter(),
    )
    weights = result.target_book.weights
    assert weights == pytest.approx({"A": 0.5, "B": 0.5, "E": -0.5, "F": -0.5})
    assert result.target_book.gross == pytest.approx(2.0)
    assert result.target_book.net == pytest.approx(0.0)
    shorts = {
        o.ticker: o.quantity
        for o in result.orders
        if o.position_effect == "open" and o.side == "sell"
    }
    # 0.5 of 10,000 equity at 10.0 a share
    assert shorts == pytest.approx({"E": 500.0, "F": 500.0})


def test_cash_book_runs_the_constructor_long_only_at_one_gross():
    for allow, strategy in ((False, _Shorter()), (True, _LongOnly())):
        result = build_orders(
            SIGNALS,
            _book(allow, neutral="dollar"),
            MarketView(as_of=AS_OF, prices=PRICES),
            strategies=lambda s, st=strategy: st,
        )
        weights = result.target_book.weights
        assert all(w > 0 for w in weights.values())
        assert result.target_book.gross <= 1.0 + 1e-9
        assert all(o.side == "buy" for o in result.orders)

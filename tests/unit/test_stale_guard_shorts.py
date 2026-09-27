"""BE-11: the stale-price guard drops opening orders on stale tickers and
always keeps closes, in a short book as in a long one."""

from __future__ import annotations

from datetime import date

from stonks.core.types import Order, Portfolio
from stonks.portfolio.orders import orders_from_targets
from stonks.portfolio.pipeline import BookInput, MarketView, build_orders
from stonks.portfolio.settings import ConstructionSettings
from stonks.production.prices import drop_stale_opens

AS_OF = date(2024, 6, 3)


class _LongShort:
    supports_short = True

    def decide(self, picks, portfolio, prices, as_of):
        targets = {t: (0.3 if r > 0 else -0.3) for r, t in picks}
        return orders_from_targets(
            targets, portfolio, prices, buffer_fraction=0.0, as_of=AS_OF,
            strategy_id="ls", allow_short=True,
        )  # fmt: skip


def test_be11_a_stale_cover_is_kept_and_a_stale_short_sale_is_dropped():
    strategy = _LongShort()
    book = BookInput(
        portfolio=Portfolio(cash=11_000.0, positions={"OLD.US": -100.0}),
        construction=ConstructionSettings(),
        allow_short=True,
    )
    market = MarketView(
        as_of=AS_OF,
        prices={"A.US": 10.0, "STALE.US": 10.0, "OLD.US": 10.0},
        buyable={"A.US"},
    )
    result = build_orders(
        {"ls": {"A.US": 0.1, "STALE.US": -0.1}}, book, market, strategies=lambda sid: strategy
    )
    legs = {(o.ticker, o.side, o.position_effect) for o in result.orders}
    assert ("OLD.US", "buy", "close") in legs
    assert not any(o.ticker == "STALE.US" for o in result.orders)
    assert result.stale_buys == ["STALE.US"]


def test_be11_without_effects_the_position_decides():
    orders = [
        Order("1", "L.US", "sell", 5.0),  # sells a long: close, kept
        Order("2", "S.US", "buy", 5.0),  # covers a short: close, kept
        Order("3", "N.US", "buy", 5.0),  # opens: dropped
        Order("4", "N2.US", "sell", 5.0),  # a short sale from flat: dropped
    ]
    kept, dropped = drop_stale_opens(orders, fresh=set(), positions={"L.US": 5.0, "S.US": -5.0})
    assert [o.client_id for o in kept] == ["1", "2"]
    assert dropped == ["N.US", "N2.US"]

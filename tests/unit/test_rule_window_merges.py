"""Tighten-only merges of look-back windows whose measure is not monotone.

A loss or a rise measured between today and exactly N bars back is not
larger over a longer window: the older point may sit on the other side.
So an override that lengthens such a window can loosen the rule, and the
merge must keep the base window (as it keeps ``atr_window``)."""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from stonks.production.rules.circuit_breaker import breaker_trips
from stonks.production.rules.settings import RuleSettings, tighter_rule_settings

AS_OF = date(2026, 3, 31)


def test_a_longer_week_window_never_loosens_the_week_loss_breaker():
    base = RuleSettings.model_validate(
        {"circuit_breaker": {"max_week_loss": 0.05, "week_sessions": 5}}
    )
    merged = tighter_rule_settings(base, {"circuit_breaker": {"week_sessions": 20}})
    # 20 sessions ago the book was low; the last 5 sessions lost 10%
    days = [AS_OF - timedelta(days=29 - i) for i in range(30)]
    values = [80.0] * 10 + [110.0] * 15 + [105.0, 102.0, 100.0, 99.0, 99.0]
    curve = list(zip(days, values, strict=True))
    assert breaker_trips(curve, AS_OF, base.circuit_breaker)
    assert breaker_trips(curve, AS_OF, merged.circuit_breaker)


def test_a_longer_spike_window_never_loosens_the_squeeze_guard():
    from stonks.production.rules.squeeze_guard import _spike

    base = RuleSettings.model_validate({"squeeze_guard": {"spike_pct": 0.2, "spike_bars": 5}})
    merged = tighter_rule_settings(base, {"squeeze_guard": {"spike_bars": 30}})

    class Ctx:
        as_of = AS_OF
        # high a month ago, then a crash and a 30% squeeze in the last 5 bars
        history = {
            "X.US": pd.DataFrame(
                {"close": [200.0] * 20 + [100.0] * 15 + [110.0, 120.0, 125.0, 128.0, 130.0]},
                index=pd.bdate_range(end=pd.Timestamp(AS_OF), periods=40),
            )
        }

    assert _spike(Ctx(), "X.US", base.squeeze_guard) is not None  # type: ignore[arg-type]
    assert _spike(Ctx(), "X.US", merged.squeeze_guard) is not None  # type: ignore[arg-type]


def test_a_longer_loss_window_never_loosens_the_loss_breaker():
    from stonks.core.types import Order
    from stonks.production.live.trades import ClosedTrade
    from stonks.production.rules.protections import _loss_breaker

    base = RuleSettings.model_validate(
        {"losing_lock": {"max_loss_pct": 0.05, "loss_window_days": 30}}
    )
    merged = tighter_rule_settings(base, {"losing_lock": {"loss_window_days": 365}})

    def closed(days_ago: int, pnl: float) -> ClosedTrade:
        return ClosedTrade(
            strategy_id="s1",
            ticker="A.US",
            exit_day=AS_OF - timedelta(days=days_ago),
            pnl=pnl,
            notional=100.0,
        )

    # lost 10% this month, after a 20% gain half a year ago
    trades = [closed(200, 20.0), closed(3, -10.0)]
    order = Order(client_id="c", ticker="A.US", side="buy", quantity=1.0)
    assert _loss_breaker(order, trades, base.losing_lock, AS_OF) is not None
    assert _loss_breaker(order, trades, merged.losing_lock, AS_OF) is not None

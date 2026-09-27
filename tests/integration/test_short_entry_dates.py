"""BE-04: the squeeze guard's price triggers see a short opened by a real
fill, through ``build_risk_context`` (not hand-made entry dates)."""

from __future__ import annotations

from datetime import date

import pandas as pd

from stonks.config import RiskPolicy
from stonks.core.types import Portfolio
from stonks.production.risk import apply_risk, build_risk_context
from stonks.production.rules.settings import RuleSettings
from stonks.store.state import SqliteState

AS_OF = date(2026, 3, 20)


def test_be04_squeeze_guard_covers_a_short_that_rallied(tmp_path, lake):
    days = pd.bdate_range(end=pd.Timestamp(AS_OF), periods=30)
    closes = [50.0] * 20 + [75.0] * 10  # +50 % after the short was opened
    lake.upsert_prices(
        pd.DataFrame(
            {
                "ticker": "X.US",
                "date": [d.date() for d in days],
                "open": closes,
                "high": closes,
                "low": closes,
                "close": closes,
                "adj_close": closes,
                "volume": 1_000_000,
            }
        )
    )
    state = SqliteState(tmp_path / "state.sqlite")
    state.migrate()
    opened = days[5].date().isoformat()
    state.execute(
        "INSERT INTO orders (client_id, ticker, side, quantity, order_type, status, portfolio_id,"
        " created_at, updated_at) VALUES ('c1', 'X.US', 'sell', 100, 'market', 'filled',"
        " 'pf_default', ?, ?)",
        [opened, opened],
    )
    state.execute(
        "INSERT INTO fills (order_client_id, ticker, quantity, price, fee, filled_at, portfolio_id)"
        " VALUES ('c1', 'X.US', 100, 50.0, 0.0, ?, 'pf_default')",
        [f"{opened}T15:00:00+00:00"],
    )
    portfolio = Portfolio(cash=20_000.0, positions={"X.US": -100.0})
    prices = {"X.US": 75.0}
    rules = RuleSettings.model_validate({"squeeze_guard": {"max_adverse_pct": 0.2}})
    policy = RiskPolicy(rules=rules)
    ctx = build_risk_context(lake, state, portfolio, prices, AS_OF, policy=policy)
    assert ctx.entry_dates == {"X.US": days[5].date()}
    result = apply_risk([], portfolio, prices, {}, policy, context=ctx, allow_short=True)
    [cover] = result.orders
    assert (cover.side, cover.quantity, cover.position_effect) == ("buy", 100.0, "close")
    state.close()

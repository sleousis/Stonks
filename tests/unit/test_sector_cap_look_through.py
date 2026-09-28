"""The sector cap can count what held funds own (roadmap 23.14). Off by
default and tighten only: turning it on never lets more through."""

from __future__ import annotations

from datetime import date, datetime

import pandas as pd
import pytest

from stonks.core.types import Portfolio
from stonks.production.risk import build_risk_context
from stonks.production.rules import registered_rules
from stonks.production.rules.settings import MERGE_RULES
from stonks.store.state import SqliteState
from tests.fixtures.risk_rules import buy, context, policy

SPY_SECTORS = {"SPY.US": {"Tech": 0.3, "Health": 0.1}}


def _rule():
    [rule] = [r for r in registered_rules() if r.name == "sector_cap"]
    return rule


def _ctx(look_through: bool, positions: dict[str, float], **kw):
    return context(
        Portfolio(cash=100_000.0 - sum(positions.values()) * 100.0, positions=positions),
        {"A.US": 100.0, "SPY.US": 100.0, "C.US": 100.0},
        policy(sector_cap={"max_weight_per_sector": 0.3, "look_through": look_through}),
        sectors={"A.US": "Tech", "C.US": "Health"},
        fund_sectors=SPY_SECTORS,
        **kw,
    )


def test_off_by_default_ignores_fund_holdings() -> None:
    # 500 SPY = 50k, 30 % tech inside it. Without look-through A may fill 30k.
    ctx = context(
        Portfolio(cash=50_000.0, positions={"SPY.US": 500.0}),
        {"A.US": 100.0, "SPY.US": 100.0},
        policy(sector_cap={"max_weight_per_sector": 0.3}),
        sectors={"A.US": "Tech"},
        fund_sectors=SPY_SECTORS,
    )
    kept, _ = _rule().apply([buy("A.US", 400)], ctx)
    assert kept[0].quantity == pytest.approx(300.0)


def test_a_stock_buy_counts_tech_inside_a_held_fund() -> None:
    kept, adj = _rule().apply([buy("A.US", 400)], _ctx(True, {"SPY.US": 500.0}))
    # cap 30k less 15k of tech inside SPY: 150 shares
    assert kept[0].quantity == pytest.approx(150.0)
    assert adj[0].rule == "max_weight_per_sector"
    assert "through funds" in adj[0].reason


def test_a_fund_buy_is_capped_by_its_largest_sector() -> None:
    # A holds 20k of tech. SPY adds 30 % tech per dollar: (30k - 20k) / 30 = 333 shares
    kept, _ = _rule().apply([buy("SPY.US", 1_000)], _ctx(True, {"A.US": 200.0}))
    assert kept[0].quantity == pytest.approx(10_000 / 30.0)


def test_look_through_never_loosens_the_plain_cap() -> None:
    """A fund with its own sector label keeps the plain cap on that label."""
    ctx = context(
        Portfolio(cash=100_000.0),
        {"SPY.US": 100.0},
        policy(sector_cap={"max_weight_per_sector": 0.3, "look_through": True}),
        sectors={"SPY.US": "Fund"},
        fund_sectors=SPY_SECTORS,
    )
    kept, _ = _rule().apply([buy("SPY.US", 1_000)], ctx)
    assert kept[0].quantity == pytest.approx(300.0)


def test_unpriced_fund_in_the_sector_drops_the_buy() -> None:
    ctx = context(
        Portfolio(cash=100_000.0, positions={"QQQ.US": 10.0}),
        {"A.US": 100.0},
        policy(sector_cap={"max_weight_per_sector": 0.3, "look_through": True}),
        sectors={"A.US": "Tech"},
        fund_sectors={"QQQ.US": {"Tech": 0.5}},
    )
    kept, adj = _rule().apply([buy("A.US", 1)], ctx)
    assert kept == []
    assert adj[0].rule == "unpriced_holding"


def test_look_through_merges_tighten_only() -> None:
    assert MERGE_RULES["sector_cap"]["look_through"](False, True) is True
    assert MERGE_RULES["sector_cap"]["look_through"](True, False) is True


def test_build_risk_context_reads_funds_only_when_asked(lake, tmp_path) -> None:
    lake.upsert_fund_holdings(
        pd.DataFrame(
            [
                {
                    "fund": "SPY.US",
                    "holding": "A.US",
                    "as_of": date(2026, 1, 2),
                    "source": "fake",
                    "weight": 0.3,
                    "sector": "Tech",
                }
            ]
        ),
        known_at=datetime(2026, 1, 3),
    )
    with SqliteState(tmp_path / "state.sqlite") as state:
        state.migrate()
        pf = Portfolio(cash=1_000.0, positions={"SPY.US": 1.0})
        on = policy(sector_cap={"max_weight_per_sector": 0.3, "look_through": True})
        off = policy(sector_cap={"max_weight_per_sector": 0.3})
        day = date(2026, 1, 5)
        ctx = build_risk_context(lake, state, pf, {"SPY.US": 10.0}, day, policy=on)
        assert ctx.fund_sectors == {"SPY.US": {"Tech": pytest.approx(0.3)}}
        ctx = build_risk_context(lake, state, pf, {"SPY.US": 10.0}, day, policy=off)
        assert ctx.fund_sectors == {}
        # a per-strategy override that turns it on is enough
        ctx = build_risk_context(lake, state, pf, {"SPY.US": 10.0}, day, policy=off, overrides=[on])
        assert "SPY.US" in ctx.fund_sectors

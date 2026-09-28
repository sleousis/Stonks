"""The trade decision store (roadmap 23.7)."""

from __future__ import annotations

from datetime import date

from stonks.portfolio.explain import TickerDecision
from stonks.production.decisions import load_decisions, prune_decisions, record_decisions


def _d(ticker: str, step: str = "rank", **kw) -> TickerDecision:
    return TickerDecision(ticker=ticker, step=step, outcome="kept_out", **kw)  # type: ignore[arg-type]


def test_record_and_load_newest_first(state):
    record_decisions(
        state,
        tick_id="t1",
        portfolio_id="pf",
        as_of=date(2026, 3, 19),
        decisions=[_d("X", strategy_id="a", strategies=("a", "b"), score=0.2)],
    )
    record_decisions(
        state,
        tick_id="t2",
        portfolio_id="pf",
        as_of=date(2026, 3, 20),
        decisions=[
            _d("X", "stale_price", strategy_id="a", strategies=("a",)),
            _d("Y", "risk_rule", detail={"rule": "sector_cap"}),
        ],
    )
    rows = load_decisions(state, "pf", ticker="x")
    assert [(r.tick_id, r.decision.step) for r in rows] == [("t2", "stale_price"), ("t1", "rank")]
    assert rows[1].decision.strategies == ("a", "b")
    assert rows[1].decision.score == 0.2
    [y] = load_decisions(state, "pf", ticker="Y")
    assert y.decision.detail == {"rule": "sector_cap"}
    assert y.as_of == date(2026, 3, 20)


def test_filters_by_strategy_tick_and_portfolio(state):
    record_decisions(
        state,
        tick_id="t1",
        portfolio_id="pf",
        as_of=date(2026, 3, 20),
        decisions=[_d("X", strategies=("a", "b")), _d("Y", strategies=("ab",))],
    )
    record_decisions(
        state, tick_id="t1", portfolio_id="other", as_of=date(2026, 3, 20), decisions=[_d("X")]
    )
    assert [r.decision.ticker for r in load_decisions(state, "pf", strategy_id="b")] == ["X"]
    assert [r.decision.ticker for r in load_decisions(state, "pf", strategy_id="ab")] == ["Y"]
    assert len(load_decisions(state, "pf", tick_id="t1")) == 2
    assert load_decisions(state, "pf", tick_id="t9") == []
    assert len(load_decisions(state, "other")) == 1


def test_rerun_of_a_tick_replaces_its_rows(state):
    for step in ("rank", "traded"):
        record_decisions(
            state,
            tick_id="t1",
            portfolio_id="pf",
            as_of=date(2026, 3, 20),
            decisions=[_d("X", step)],
        )
    [row] = load_decisions(state, "pf")
    assert row.decision.step == "traded"


def test_prune_drops_old_days(state):
    record_decisions(
        state, tick_id="t0", portfolio_id="pf", as_of=date(2025, 1, 2), decisions=[_d("X")]
    )
    record_decisions(
        state, tick_id="t1", portfolio_id="pf", as_of=date(2026, 3, 20), decisions=[_d("X")]
    )
    assert prune_decisions(state, before=date(2026, 1, 1)) == 1
    assert [r.tick_id for r in load_decisions(state, "pf")] == ["t1"]


def test_limit_and_offset(state):
    record_decisions(
        state,
        tick_id="t1",
        portfolio_id="pf",
        as_of=date(2026, 3, 20),
        decisions=[_d(t) for t in "ABCDE"],
    )
    assert len(load_decisions(state, "pf", limit=2)) == 2
    assert [r.decision.ticker for r in load_decisions(state, "pf", limit=2, offset=3)] == ["D", "E"]

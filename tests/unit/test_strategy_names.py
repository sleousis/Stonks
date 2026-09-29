"""Strategy names people read (UX-27): views that carry a strategy id also
carry ``strategy_name``, a starter's plain title, so no page shows
``starter_trend`` where it could show "Starter: trend following"."""

from __future__ import annotations

from datetime import UTC, date, datetime

from stonks.app.leaderboard import LeaderboardRow, paper_figures
from stonks.app.operations import ShadowDecisionView, ShadowPnlSummary
from stonks.app.orders import OrderView
from stonks.app.strategy_names import strategy_title
from stonks.app.tick_summary import ShadowOutcomeView, TickSummary

TREND = "Starter: trend following"


def test_strategy_title_is_the_starter_title() -> None:
    assert strategy_title("starter_trend") == TREND


def test_strategy_title_is_none_for_other_strategies() -> None:
    assert strategy_title("bah_aaa") is None
    assert strategy_title(None) is None
    assert strategy_title("") is None


def _order(strategy_id: str | None) -> OrderView:
    return OrderView(
        client_id="c1",
        tick_id=None,
        strategy_id=strategy_id,
        ticker="SPY.US",
        side="buy",
        quantity=1,
        order_type="market",
        limit_price=None,
        status="filled",
        broker_order_id=None,
        created_at="2026-09-01",
        updated_at="2026-09-01",
    )


def test_order_view_serialises_the_strategy_name() -> None:
    assert _order("starter_trend").model_dump()["strategy_name"] == TREND
    assert _order("bah_aaa").model_dump()["strategy_name"] is None
    assert _order(None).model_dump()["strategy_name"] is None


def test_shadow_views_carry_the_strategy_name() -> None:
    summary = ShadowPnlSummary(
        strategy_id="starter_trend",
        status="shadow",
        days=1,
        first_day=None,
        latest_day=None,
        total_value=None,
        cumulative_return=None,
        max_drawdown=None,
    )
    decision = ShadowDecisionView(
        id=1,
        tick_id="t",
        strategy_id="starter_trend",
        as_of=date(2026, 9, 1),
        ticker="SPY.US",
        side="buy",
        quantity=1,
        price=None,
        status="filled",
        created_at=datetime(2026, 9, 1, tzinfo=UTC),
    )
    outcome = ShadowOutcomeView(strategy_id="starter_trend", status="ok")
    for view in (summary, decision, outcome):
        assert view.model_dump()["strategy_name"] == TREND


def test_leaderboard_row_carries_the_strategy_name() -> None:
    row = LeaderboardRow(
        rank=1,
        strategy_id="starter_momentum",
        class_path="x:Y",
        status="shadow",
        paper=paper_figures([], 0),
        book_trades=0,
        live_since=None,
        survival_passed=0,
        survival_total=0,
        golive_passed=None,
    )
    assert row.model_dump()["strategy_name"] == "Starter: momentum"


def test_tick_summary_names_the_winner_and_the_exit_strategy() -> None:
    summary = TickSummary(winner_strategy_id="starter_trend", exit_strategy_id="bah_aaa")
    dumped = summary.model_dump()
    assert dumped["winner_strategy_name"] == TREND
    assert dumped["exit_strategy_name"] is None


def test_journal_trades_name_their_sleeve() -> None:
    from stonks.app.journal import JournalTradeView

    fields = JournalTradeView.model_fields
    data = dict.fromkeys(fields)
    data.update(
        leg_id="l1",
        trade_id=1,
        ticker="SPY.US",
        side="long",
        sleeve="starter_trend",
        origin="strategy",
        entry_client_id="c1",
        entry_at=datetime(2026, 9, 1, tzinfo=UTC),
        quantity=1,
        entry_price=1,
        exit_price=1,
        is_open=True,
        holding_days=0,
        pnl=0,
        return_pct=0,
        fees=0,
        dividends=0,
        currency="USD",
        tags=[],
        mistakes=[],
    )
    assert JournalTradeView.model_validate(data).sleeve_name == TREND
    data["sleeve"] = "manual"
    assert JournalTradeView.model_validate(data).sleeve_name is None


def test_a_passed_strategy_name_is_replaced_by_the_derived_one() -> None:
    order = _order("bah_aaa").model_copy(update={}).model_dump() | {"strategy_name": "Fake"}
    assert OrderView.model_validate(order).strategy_name is None


def test_tick_result_names_the_winner() -> None:
    from stonks.app.ticks import TickResultView

    result = TickResultView(
        tick_id="t",
        status="done",
        winner_strategy_id="starter_trend",
        orders_placed=0,
        fills=0,
        dry_run=False,
    )
    assert result.winner_strategy_name == TREND

"""Transaction cost analysis at its edges (BL-32): bad inputs, groupings
of orders with no portfolio or decision time, the journal's filters and
unreadable rows, notes that are missing or written by someone else, and
benchmark refreshes with no next session."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pandas as pd
import pytest

from stonks.backtest.costs import FixedCostModel, Trade
from stonks.production.tca import (
    FillLeg,
    JournalError,
    OrderTca,
    add_note,
    compute_shortfall,
    expected_cost_bps,
    get_note,
    group_key,
    journal,
    refresh_benchmarks,
    summarize,
    update_note,
)

PF = "pf_default"
DAY = date(2026, 3, 2)


def order(state, cid, *, decided="2026-03-02T20:00:00+00:00", context=None, created=None,
          price=100.0, status="filled", ticker="X.US"):  # fmt: skip
    state.execute(
        "INSERT INTO orders (client_id, ticker, side, quantity, order_type, status, created_at,"
        " updated_at, portfolio_id, decision_price, decided_at, decision_context_json)"
        " VALUES (?, ?, 'buy', 10, 'market', ?, ?, ?, ?, ?, ?, ?)",
        [cid, ticker, status, created or decided or "2026-03-02T20:00:00+00:00", "x", PF, price,
         decided, context],
    )  # fmt: skip


def row(portfolio="pf", decided=datetime(2026, 3, 4, tzinfo=UTC)) -> OrderTca:
    return OrderTca(
        client_id="c", portfolio_id=portfolio, strategy_id=None, ticker="X", side="buy",
        status="filled", decided_at=decided,
        shortfall=compute_shortfall("buy", 100.0, 1.0, [FillLeg(1.0, 101.0, 0.0, 100.0)]),
    )  # fmt: skip


# ---- the math ------------------------------------------------------------------------


@pytest.mark.parametrize("price", [0.0, -1.0])
def test_a_decision_price_must_be_positive(price):
    with pytest.raises(ValueError, match="decision_price must be positive"):
        compute_shortfall("buy", price, 1.0, [])


@pytest.mark.parametrize(("qty", "price"), [(0.0, 100.0), (10.0, 0.0)])
def test_an_empty_trade_has_no_expected_cost(qty, price):
    trade = Trade(ticker="X", side="buy", quantity=qty, price=price)
    assert expected_cost_bps(FixedCostModel(slippage_bps=10.0), trade) is None


def test_groups_for_orders_with_no_portfolio_or_decision_time():
    assert group_key(row(portfolio=None), "portfolio") == "(none)"
    assert group_key(row(), "portfolio") == "pf"
    assert group_key(row(decided=None), "day") == "(unknown)"
    assert group_key(row(), "day") == "2026-03-04"
    assert [g.key for g in summarize([row(), row(decided=None)], "month")] == [
        "(unknown)",
        "2026-03",
    ]


def test_an_unknown_grouping_is_refused():
    with pytest.raises(ValueError, match="unknown grouping 'year'"):
        summarize([row()], "year")  # type: ignore[arg-type]


# ---- the journal ----------------------------------------------------------------------


def test_the_journal_filters_by_decision_time(state):
    order(state, "old", decided="2026-02-27T20:00:00+00:00")
    order(state, "new")
    entries, total = journal(state, PF, since=DAY)
    assert total == 1 and [e.client_id for e in entries] == ["new"]


def test_an_unreadable_context_is_shown_as_none(state):
    order(state, "bad", context="not json")
    order(state, "list", context="[1, 2]")
    order(state, "good", context='{"trigger": "manual"}')
    entries, _ = journal(state, PF)
    assert {e.client_id: e.context for e in entries} == {"bad": None, "list": None,
                                                         "good": {"trigger": "manual"}}  # fmt: skip


# ---- notes ----------------------------------------------------------------------------


def test_a_note_on_a_missing_order_is_refused(state):
    with pytest.raises(JournalError, match="order 'nope' not found"):
        add_note(state, "nope", PF, author="u", note="hi")


def test_a_note_of_another_portfolio_reads_as_missing(state):
    order(state, "o1")
    note = add_note(state, "o1", PF, author="u", note="hi", now=datetime(2026, 3, 2, 21, 0))
    assert note.created_at == "2026-03-02T21:00:00+00:00"  # a naive time is taken as UTC
    assert get_note(state, note.id, None).note == "hi"
    with pytest.raises(JournalError, match=f"note {note.id} not found"):
        get_note(state, note.id, "pf_other")


def test_only_the_author_may_change_a_note(state):
    order(state, "o1")
    note = add_note(state, "o1", PF, author="u", note="hi")
    with pytest.raises(JournalError, match="not found"):
        update_note(state, note.id, PF, author="someone else", note="mine now")
    assert update_note(state, note.id, PF, author="u", note="edited").note == "edited"


# ---- benchmarks -------------------------------------------------------------------------


def test_no_session_after_the_decision_leaves_the_benchmarks_empty(state, lake):
    order(state, "o1")
    lake.upsert_prices(pd.DataFrame({
        "ticker": ["X.US"], "date": pd.to_datetime(["2026-03-02"]), "open": [100.0],
        "high": [101.0], "low": [99.0], "close": [100.0], "adj_close": [100.0], "volume": [1.0],
    }))  # fmt: skip
    assert refresh_benchmarks(state, lake) == 0
    [r] = state.sql("SELECT benchmark_price, post_close_price FROM orders")
    assert (r["benchmark_price"], r["post_close_price"]) == (None, None)


def test_a_next_session_without_prices_is_skipped(state, lake):
    order(state, "o1")
    lake.upsert_prices(pd.DataFrame({
        "ticker": ["X.US"], "date": pd.to_datetime(["2026-03-03"]), "open": [float("nan")],
        "high": [1.0], "low": [1.0], "close": [0.0], "adj_close": [1.0], "volume": [1.0],
    }))  # fmt: skip
    assert refresh_benchmarks(state, lake) == 0


def test_a_next_session_with_only_a_close_fills_the_post_close(state, lake):
    order(state, "o1")
    lake.upsert_prices(pd.DataFrame({
        "ticker": ["X.US"], "date": pd.to_datetime(["2026-03-03"]), "open": [float("nan")],
        "high": [1.0], "low": [1.0], "close": [102.0], "adj_close": [1.0], "volume": [1.0],
    }))  # fmt: skip
    assert refresh_benchmarks(state, lake) == 1
    [r] = state.sql("SELECT benchmark_price, post_close_price FROM orders")
    assert (r["benchmark_price"], r["post_close_price"]) == (None, 102.0)

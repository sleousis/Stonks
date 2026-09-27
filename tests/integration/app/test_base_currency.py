"""Values and P&L in the portfolio's base currency (roadmap 20.5): the
book, the daily P&L and insights convert foreign holdings at stored FX
rates, keep cash as is, and never guess a missing rate."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from stonks.store.lake import DuckDBLake
from stonks.store.state import SqliteState


def _set_currency(settings, ticker: str, currency: str) -> None:
    lake = DuckDBLake(settings.lake.path)
    lake.con.execute("UPDATE instruments SET currency = ? WHERE id = ?", [currency, ticker])
    lake.close()


def _add_rate(settings, base: str, quote: str, day: date, rate: float) -> None:
    lake = DuckDBLake(settings.lake.path)
    lake.upsert_fx_rates(
        pd.DataFrame(
            [
                {
                    "base_currency": base,
                    "quote_currency": quote,
                    "observation_date": day,
                    "rate": rate,
                    "source": "fake",
                }
            ]
        )
    )
    lake.close()


@pytest.fixture
def foreign(settings, seeded):
    """pf_default holds UP.US; make it a EUR listing in a USD book."""
    _set_currency(settings, "UP.US", "EUR")
    with SqliteState(settings.state.path) as state:
        state.execute("UPDATE portfolios SET base_currency = 'USD' WHERE id = 'pf_default'")
    return seeded


def test_single_currency_book_base_totals_equal_totals(services):
    view = services.portfolio.current("pf_default")
    assert view.base_currency == "USD"
    assert view.fx_missing == []
    assert view.total_value_base == pytest.approx(view.total_value)
    pnl = services.operations.pnl(portfolio_id="pf_default")
    assert pnl.base_currency == "USD"
    assert [r.total_value for r in pnl.base_rows or []] == [r.total_value for r in pnl.rows]


def test_missing_rate_is_reported_not_guessed(settings, foreign, services):
    view = services.portfolio.current("pf_default")
    assert view.fx_missing == ["EUR"]
    assert view.total_value_base is None and view.positions_value_base is None
    assert view.positions[0].market_value_base is None
    assert view.total_value == pytest.approx(view.cash + view.positions_value)  # unchanged
    pnl = services.operations.pnl(portfolio_id="pf_default")
    assert pnl.base_rows is None and pnl.fx_missing == ["EUR"]
    insights = services.insights.insights("pf_default")
    assert insights.total_value_base is None and insights.fx_missing == ["EUR"]
    assert any("no FX rate for EUR" in n for n in insights.notes)


def test_foreign_holding_converts_at_the_rate(settings, foreign, services):
    _add_rate(settings, "EUR", "USD", date(2026, 1, 1), 2.0)
    view = services.portfolio.current("pf_default")
    pos = view.positions[0]
    assert pos.market_value_base == pytest.approx(pos.market_value * 2.0)
    assert view.total_value_base == pytest.approx(view.cash + pos.market_value * 2.0)
    pnl = services.operations.pnl(portfolio_id="pf_default")
    assert pnl.fx_missing == []
    row, base = pnl.rows[-1], (pnl.base_rows or [])[-1]
    # cash stays as is, the holding doubles
    snapshot = services.portfolio.snapshots(limit=1, offset=0, portfolio_id="pf_default").items[0]
    held_value = row.total_value - snapshot.cash
    assert base.total_value == pytest.approx(snapshot.cash + 2.0 * held_value)
    insights = services.insights.insights("pf_default")
    assert insights.total_value_base == pytest.approx(insights.cash + 2.0 * pos.market_value)


def test_tca_summary_money_in_base(settings, foreign, services):
    from stonks.app.tca import TcaService

    tca = TcaService(services.context)
    before = tca.summary("pf_default")
    assert before.base_currency == "USD" and before.fx_missing == ["EUR"]
    assert before.groups_base and before.groups_base[0].filled_notional is None
    _add_rate(settings, "EUR", "USD", date(2026, 1, 1), 2.0)
    after = tca.summary("pf_default")
    group, money = after.groups[0], after.groups_base[0]
    assert after.fx_missing == []
    assert money.filled_notional == pytest.approx(group.filled_notional * 2.0)

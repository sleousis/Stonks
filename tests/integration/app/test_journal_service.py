"""JournalService over a real lake (roadmap 23.3): excursions from stored
daily bars, the open-leg mark, and base-currency conversion."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from stonks.accounts import DEFAULT_PORTFOLIO_ID, Scope
from stonks.app.context import AppContext
from stonks.app.errors import NotFoundError, ValidationError
from stonks.app.journal import AnnotationRequest, JournalService, PlaybookCreate, TradeFilter
from stonks.store.lake import DuckDBLake
from stonks.store.state import SqliteState

NOW = datetime(2026, 3, 31, tzinfo=UTC)
OPERATOR = Scope.service("cli")


def _fill(state, pf, cid, ticker, side, qty, price, ts) -> int:
    state.execute(
        "INSERT INTO orders (client_id, ticker, side, quantity, order_type, status, created_at,"
        " updated_at, portfolio_id, origin) VALUES (?, ?, ?, ?, 'market', 'filled', ?, ?, ?,"
        " 'manual')",
        [cid, ticker, side, qty, ts, ts, pf],
    )
    cur = state.execute(
        "INSERT INTO fills (order_client_id, ticker, quantity, price, fee, filled_at, portfolio_id)"
        " VALUES (?, ?, ?, ?, 0, ?, ?)",
        [cid, ticker, qty, price, ts, pf],
    )
    return int(cur.lastrowid or 0)


@pytest.fixture
def service(settings, seeded):
    return JournalService(AppContext(settings), now=NOW)


def test_default_book_trades_have_excursions_from_lake_bars(service):
    page = service.trades(DEFAULT_PORTFOLIO_ID)
    assert page.total >= 1
    leg = page.items[0]
    assert leg.ticker == "UP.US" and leg.is_open
    assert leg.sleeve == "bah_active"
    assert leg.mfe_pct is not None and leg.mfe_pct > 0  # UP.US rises every day
    assert leg.mae_pct is not None and leg.mae_pct <= 0
    assert leg.exit_price > leg.entry_price


def test_manual_round_trip_in_a_foreign_base_currency_needs_an_fx_rate(settings, seeded):
    lake = DuckDBLake(settings.lake.path)
    try:
        lake.con.execute("UPDATE instruments SET currency = 'USD' WHERE id = 'FLAT.US'")
    finally:
        lake.close()
    with SqliteState(settings.state.path) as state:
        state.execute(
            "UPDATE portfolios SET base_currency = 'EUR' WHERE id = ?", [DEFAULT_PORTFOLIO_ID]
        )
        _fill(state, DEFAULT_PORTFOLIO_ID, "m1", "FLAT.US", "buy", 4, 50.0, "2026-03-23T15:00:00")
        _fill(state, DEFAULT_PORTFOLIO_ID, "m2", "FLAT.US", "sell", 4, 52.0, "2026-03-24T15:00:00")
    service = JournalService(AppContext(settings), now=NOW)
    page = service.trades(DEFAULT_PORTFOLIO_ID, TradeFilter(sleeve="manual"))
    [leg] = page.items
    assert leg.pnl == pytest.approx(8.0)
    assert (leg.currency, leg.pnl_base) == ("USD", None)
    cal = service.calendar(DEFAULT_PORTFOLIO_ID)
    assert cal.unconverted == 1 and cal.fx_missing == ["USD"]
    # flat bars: high 50.5 and low 49.5 around a 50 entry
    assert leg.mae_pct == pytest.approx(-0.01)
    assert leg.exit_efficiency == pytest.approx(1.0)


def test_operator_reviews_and_errors(settings, seeded, service):
    trade_id = service.trades(DEFAULT_PORTFOLIO_ID).items[0].trade_id
    pb = service.create_playbook(OPERATOR, PlaybookCreate(name="Trend"))
    saved = service.annotate(
        OPERATOR,
        DEFAULT_PORTFOLIO_ID,
        trade_id,
        AnnotationRequest(tags=["core"], playbook_id=pb.id, followed_plan=True),
    )
    assert saved.updated_by == "service:cli"
    assert [p.name for p in service.playbooks(OPERATOR)] == ["Trend"]
    detail = service.trade(DEFAULT_PORTFOLIO_ID, trade_id)
    assert detail.playbook_name == "Trend" and detail.tags == ["core"]
    with pytest.raises(NotFoundError):
        service.annotate(OPERATOR, DEFAULT_PORTFOLIO_ID, 999_999, AnnotationRequest())
    with pytest.raises(ValidationError):
        service.annotate(
            OPERATOR, DEFAULT_PORTFOLIO_ID, trade_id, AnnotationRequest(tags=["x" * 41])
        )
    with pytest.raises(ValidationError):
        service.breakdown(DEFAULT_PORTFOLIO_ID, by="colour")  # type: ignore[arg-type]

"""The paper soak report (roadmap 19.11, ``production.soak``)."""

from __future__ import annotations

from datetime import date

import pytest

from stonks.production.soak import soak_report
from stonks.store.state import SqliteState

BROKER = "pf_broker"
MODEL = "pf_model"


@pytest.fixture
def state(tmp_path):
    s = SqliteState(tmp_path / "state.sqlite")
    s.migrate()
    s.execute(
        "INSERT INTO portfolios (id, owner_id, name, kind, created_at)"
        " VALUES (?, 'usr_owner', 'IBKR paper', 'broker', '2026-09-01')",
        [BROKER],
    )
    s.execute(
        "INSERT INTO portfolios (id, owner_id, name, kind, created_at, paper_of)"
        " VALUES (?, 'usr_owner', 'model', 'simulated', '2026-09-01', ?)",
        [MODEL, BROKER],
    )
    yield s
    s.close()


def order(
    state: SqliteState,
    cid: str,
    *,
    day: str,
    ticker: str = "AAPL.US",
    side: str = "buy",
    qty: float = 10,
    status: str = "filled",
    decision: float | None = 100.0,
    portfolio: str = BROKER,
    reason: str | None = None,
    order_state: str | None = None,
    fill_price: float | None = None,
    fill_qty: float | None = None,
) -> None:
    state.execute(
        "INSERT INTO orders (client_id, ticker, side, quantity, order_type, status,"
        " status_reason, created_at, updated_at, portfolio_id, decision_price, decided_at,"
        " state) VALUES (?, ?, ?, ?, 'market', ?, ?, ?, ?, ?, ?, ?, ?)",
        [cid, ticker, side, qty, status, reason, f"{day}T21:00:00+00:00",
         f"{day}T21:00:00+00:00", portfolio, decision, day, order_state or status],
    )  # fmt: skip
    if fill_price is not None:
        state.execute(
            "INSERT INTO fills (order_client_id, ticker, quantity, price, fee, filled_at,"
            " portfolio_id) VALUES (?, ?, ?, ?, 0, ?, ?)",
            [cid, ticker, fill_qty if fill_qty is not None else qty, fill_price,
             f"{day}T21:30:00+00:00", portfolio],
        )  # fmt: skip


def test_empty_book_is_not_clean_and_says_why(state):
    report = soak_report(state, portfolio_id=BROKER, days=20, end=date(2026, 9, 25))
    assert report.days_observed == 0
    assert report.orders == 0
    assert not report.clean
    assert any("0 of 20" in f for f in report.findings)


def test_counts_statuses_and_reject_reasons(state):
    order(state, "a", day="2026-09-22", fill_price=100.5)
    order(state, "b", day="2026-09-22", status="rejected", reason="201 margin")
    order(state, "c", day="2026-09-23", status="rejected", reason="201 margin")
    order(state, "d", day="2026-09-23", status="cancelled")
    order(state, "e", day="2026-09-24", status="pending", order_state="unknown")
    report = soak_report(state, portfolio_id=BROKER, days=5, end=date(2026, 9, 25))
    assert report.days_observed == 3
    assert (report.orders, report.filled, report.rejected, report.cancelled) == (5, 1, 2, 1)
    assert report.unknown == 1
    assert report.reject_rate == pytest.approx(0.4)
    assert report.reject_reasons == {"201 margin": 2}


def test_window_keeps_the_last_n_trading_days(state):
    for i, day in enumerate(["2026-09-21", "2026-09-22", "2026-09-23", "2026-09-24"]):
        order(state, f"o{i}", day=day, fill_price=100.0)
    order(state, "late", day="2026-09-26", fill_price=100.0)
    report = soak_report(state, portfolio_id=BROKER, days=2, end=date(2026, 9, 25))
    assert (report.start, report.end) == (date(2026, 9, 23), date(2026, 9, 25))
    assert report.orders == 2


def test_slippage_is_signed_cost_in_bps(state):
    order(state, "buy", day="2026-09-22", side="buy", fill_price=101.0)  # +100 bps cost
    order(state, "sell", day="2026-09-22", side="sell", fill_price=99.5)  # +50 bps cost
    order(state, "nodec", day="2026-09-22", decision=None, fill_price=90.0)
    report = soak_report(state, portfolio_id=BROKER, days=5, end=date(2026, 9, 25))
    assert report.slippage.count == 2
    assert report.slippage.mean_bps == pytest.approx(75.0)
    assert report.slippage.worst_bps == pytest.approx(100.0)


def test_compares_fills_with_the_model_book(state):
    order(state, "b1", day="2026-09-22", qty=10, fill_price=101.0)
    order(state, "m1", day="2026-09-22", qty=10, fill_price=100.0, portfolio=MODEL)
    order(state, "b2", day="2026-09-23", ticker="MSFT.US", qty=5, fill_price=50.0, fill_qty=3)
    order(state, "m2", day="2026-09-23", ticker="MSFT.US", qty=5, fill_price=50.0,
          portfolio=MODEL)  # fmt: skip
    order(state, "m3", day="2026-09-23", ticker="NVDA.US", qty=1, fill_price=10.0,
          portfolio=MODEL)  # fmt: skip
    report = soak_report(state, portfolio_id=BROKER, days=5, end=date(2026, 9, 25))
    vs = report.vs_model
    assert vs is not None and vs.model_portfolio_id == MODEL
    assert (vs.matched, vs.broker_only, vs.model_only) == (2, 0, 1)
    assert vs.quantity_gap == pytest.approx(2.0)
    assert vs.price_gap_bps == pytest.approx(50.0)  # (100 + 0) / 2


def test_no_model_book_leaves_the_comparison_out(state):
    state.execute("UPDATE portfolios SET paper_of = NULL WHERE id = ?", [MODEL])
    order(state, "b1", day="2026-09-22", fill_price=101.0)
    report = soak_report(state, portfolio_id=BROKER, days=5, end=date(2026, 9, 25))
    assert report.vs_model is None
    explicit = soak_report(
        state, portfolio_id=BROKER, days=5, end=date(2026, 9, 25), model_portfolio_id=MODEL
    )
    assert explicit.vs_model is not None


def test_gateway_outages_and_recoveries_come_from_notifications(state):
    for key, day in [
        ("gateway_down:paper:2026-09-22:admins", "2026-09-22"),
        ("gateway_down:paper:2026-09-22:pf_broker", "2026-09-22"),
        ("gateway_up:paper:2026-09-22:admins", "2026-09-22"),
        ("gateway_down:paper:2026-09-10:admins", "2026-09-10"),  # before the window
    ]:
        state.execute(
            "INSERT INTO notification_outbox (user_id, category, level, urgency, title, body,"
            " dedupe_key, created_at) VALUES ('usr_owner', 'risk', 'error', 'high', 't', 'b',"
            " ?, ?)",
            [key, f"{day}T10:00:00+00:00"],
        )
    order(state, "a", day="2026-09-22", fill_price=100.0)
    report = soak_report(state, portfolio_id=BROKER, days=5, end=date(2026, 9, 25))
    assert (report.outages, report.reconnects) == (1, 1)


def test_broker_drift_halts_and_reconcile_reports(state):
    order(state, "a", day="2026-09-22", fill_price=100.0)
    state.execute(
        "INSERT INTO risk_halts (kind, scope, portfolio_id, halt, reason, tripped_by,"
        " tripped_at) VALUES ('broker_drift', 'portfolio', ?, 'all', 'drift', 'system',"
        " '2026-09-23T10:00:00+00:00')",
        [BROKER],
    )
    state.execute(
        "INSERT INTO reconcile_reports (id, portfolio_id, kind, as_of, taken_at, status)"
        " VALUES ('rec_1', ?, 'eod', '2026-09-22', '2026-09-22T22:00:00+00:00', 'clean'),"
        " ('rec_2', ?, 'eod', '2026-09-23', '2026-09-23T22:00:00+00:00', 'drift'),"
        " ('rec_3', ?, 'sod', '2026-09-24', '2026-09-24T12:00:00+00:00', 'outage')",
        [BROKER, BROKER, BROKER],
    )
    report = soak_report(state, portfolio_id=BROKER, days=5, end=date(2026, 9, 25))
    assert report.drift_halts == 1
    assert report.drift is not None
    assert (report.drift.reports, report.drift.with_drift) == (3, 1)
    assert not report.clean


def test_no_reconcile_reports_in_the_window_counts_zero(state):
    order(state, "a", day="2026-09-22", fill_price=100.0)
    report = soak_report(state, portfolio_id=BROKER, days=1, end=date(2026, 9, 25))
    assert report.drift is not None
    assert (report.drift.reports, report.drift.with_drift) == (0, 0)


def test_a_clean_soak(state):
    days = ["2026-09-21", "2026-09-22", "2026-09-23"]
    for i, day in enumerate(days):
        order(state, f"b{i}", day=day, fill_price=100.1)
        order(state, f"m{i}", day=day, fill_price=100.0, portfolio=MODEL)
    report = soak_report(state, portfolio_id=BROKER, days=3, end=date(2026, 9, 25))
    assert report.findings == []
    assert report.clean
    data = report.to_dict()
    assert data["clean"] is True and data["days_observed"] == 3

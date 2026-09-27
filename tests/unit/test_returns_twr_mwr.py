"""Time- and money-weighted returns (roadmap 20.5): a deposit is not profit."""

from __future__ import annotations

from datetime import date

import pytest

from stonks.insights.returns import Flow, mwr, net_flows, twr, twr_since

D = [date(2026, 1, d) for d in range(1, 11)]


def test_a_deposit_mid_period_is_not_return():
    points = [(D[0], 100.0), (D[1], 200.0)]
    flows = [Flow(D[1], 100.0)]
    assert twr(points, flows) == pytest.approx(0.0)
    assert mwr(points, flows) == pytest.approx(0.0, abs=1e-9)
    assert net_flows(points, flows) == 100.0


def test_without_flows_twr_is_the_plain_change():
    points = [(D[0], 100.0), (D[1], 110.0), (D[2], 121.0)]
    assert twr(points, []) == pytest.approx(0.21)


def test_twr_chains_sub_periods_around_a_withdrawal():
    # +10%, then 50 out, then +10% again
    points = [(D[0], 100.0), (D[1], 110.0), (D[2], 66.0)]
    assert twr(points, [Flow(D[2], -50.0)]) == pytest.approx(0.21)


def test_a_flow_on_the_first_day_is_starting_capital():
    points = [(D[0], 200.0), (D[1], 220.0)]
    assert twr(points, [Flow(D[0], 100.0)]) == pytest.approx(0.10)
    assert net_flows(points, [Flow(D[0], 100.0)]) == 0.0


def test_flows_between_values_land_on_the_next_value():
    points = [(D[0], 100.0), (D[5], 150.0)]
    assert twr(points, [Flow(D[3], 50.0)]) == pytest.approx(0.0)


def test_edge_cases_give_none():
    assert twr([], []) is None and mwr([], []) is None
    assert twr([(D[0], 100.0)], []) is None
    assert mwr([(D[0], 0.0), (D[1], 10.0)], []) is None
    assert twr([(D[0], 100.0), (D[1], 0.0), (D[2], 10.0)], []) is None  # zero base
    assert mwr([(D[0], 100.0), (D[0], 110.0)], []) is None  # no time passed


def test_mwr_is_annualized():
    points = [(date(2025, 1, 1), 100.0), (date(2026, 1, 1), 110.0)]
    assert mwr(points, []) == pytest.approx(0.10, rel=1e-6)


def test_mwr_weighs_money_timing():
    # a big deposit just before a loss hurts the money-weighted return more
    points = [(date(2025, 1, 1), 100.0), (date(2025, 7, 1), 100.0), (date(2026, 1, 1), 900.0)]
    flows = [Flow(date(2025, 7, 1), 900.0)]
    points[1] = (date(2025, 7, 1), 1000.0)
    out = mwr(points, flows)
    assert out is not None and out < 0
    assert twr(points, flows) == pytest.approx(-0.10)


def test_twr_since_starts_at_the_last_value_on_or_before():
    points = [(D[0], 100.0), (D[2], 110.0), (D[4], 121.0)]
    assert twr_since(points, [], D[3]) == pytest.approx(0.10)
    assert twr_since(points, [], None) is None


def test_period_pnl_takes_a_deposit_out_of_the_return():
    from stonks.insights.pnl import period_pnl

    points = [(D[0], 1000.0), (D[1], 1000.0), (D[2], 1500.0)]
    rows = {r.period: r for r in period_pnl(points, [Flow(D[2], 500.0)])}
    inception = rows["inception"]
    assert inception.change == 500.0 and inception.change_pct == pytest.approx(0.5)
    assert inception.twr == pytest.approx(0.0) and inception.net_flows == 500.0
    assert rows["1d"].twr == pytest.approx(0.0)


def test_flows_come_from_the_ledger_and_broker_activities(tmp_path):
    from stonks.insights.flows import external_flows, recorded_flows
    from stonks.store.state import SqliteState

    with SqliteState(tmp_path / "s.sqlite") as state:
        state.migrate()
        state.execute(
            "INSERT INTO portfolios (id, owner_id, name, kind, created_at)"
            " VALUES ('pf_b', 'usr_owner', 'b', 'broker', 'x')"
        )
        cols = {r["name"] for r in state.sql("PRAGMA table_info(broker_connections)")}
        values = {
            "id": "c1",
            "user_id": "usr_owner",
            "provider": "fake",
            "label": "x",
            "status": "active",
            "created_at": "x",
            "updated_at": "x",
        }
        values = {k: v for k, v in values.items() if k in cols}
        state.execute(
            f"INSERT INTO broker_connections ({', '.join(values)})"
            f" VALUES ({', '.join('?' for _ in values)})",
            list(values.values()),
        )
        for i, (kind, amount) in enumerate(
            [("deposit", 100.0), ("withdrawal", -40.0), ("dividend", 5.0)]
        ):
            state.execute(
                "INSERT INTO broker_activities (connection_id, portfolio_id, external_account_id,"
                " provider_activity_id, kind, amount, trade_date, synced_at)"
                " VALUES ('c1', 'pf_b', 'a', ?, ?, ?, ?, 'x')",
                [str(i), kind, amount, f"2026-01-0{i + 1}"],
            )
        state.execute(
            "INSERT INTO portfolio_cash_flows (portfolio_id, flow_date, kind, amount, created_at,"
            " created_by) VALUES ('pf_b', '2026-01-05', 'deposit', 7, 'x', 'user:x')"
        )
        flows = external_flows(state, "pf_b")
        assert [(f.day.day, f.amount) for f in flows] == [(1, 100.0), (2, -40.0), (5, 7.0)]
        assert {f.source for f in recorded_flows(state, "pf_b")} == {"broker", "manual"}

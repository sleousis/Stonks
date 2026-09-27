"""Cash and broker statement drift (roadmap 19.15): pure diffs."""

from __future__ import annotations

from datetime import date

import pytest

from stonks.execution.drift import (
    DRIFT_KINDS,
    BookedFill,
    CashFlows,
    StatementExecution,
    cash_drift,
    cash_tolerance,
    report_status,
    statement_drift,
)


def kinds(items):
    return sorted((i.kind, i.key) for i in items)


# ---- cash -----------------------------------------------------------------------------------


def test_new_kinds_are_listed():
    for kind in (
        "cash",
        "settled_cash",
        "statement_missing_execution",
        "statement_extra_execution",
        "statement_quantity",
        "statement_commission",
    ):
        assert kind in DRIFT_KINDS


def test_cash_flows_add_up():
    flows = CashFlows(trades=-2000.0, fees=-1.0, dividends=5.0, external=100.0)
    assert flows.total == pytest.approx(-1896.0)
    assert CashFlows().total == 0.0


def test_the_tolerance_is_the_larger_of_the_floor_and_the_fraction():
    assert cash_tolerance(100_000.0, minimum=1.0, fraction=0.0001) == pytest.approx(10.0)
    assert cash_tolerance(5_000.0, minimum=1.0, fraction=0.0001) == pytest.approx(1.0)
    assert cash_tolerance(-100_000.0, minimum=1.0, fraction=0.0001) == pytest.approx(10.0)


def test_a_cash_change_stonks_explains_is_clean():
    flows = CashFlows(trades=-2000.0, fees=-1.0)
    assert cash_drift("cash", "USD", 50_000.0, 47_999.0, flows, tolerance=1.0) == []


def test_a_difference_inside_the_tolerance_is_clean():
    flows = CashFlows(trades=-2000.0)
    assert cash_drift("cash", "USD", 50_000.0, 47_999.0, flows, tolerance=1.0) == []
    [item] = cash_drift("cash", "USD", 50_000.0, 47_998.99, flows, tolerance=1.0)
    assert item.kind == "cash"


def test_an_unexplained_cash_change_warns_by_default():
    flows = CashFlows(trades=-2000.0, fees=-1.0, dividends=3.0, external=0.0)
    [item] = cash_drift("cash", "USD", 50_000.0, 47_500.0, flows, tolerance=1.0)
    assert (item.kind, item.key) == ("cash", "USD")
    assert item.ours == pytest.approx(-1998.0)
    assert item.broker == pytest.approx(-2500.0)
    assert not item.material and not item.explained
    assert "-502.00 unexplained" in item.detail
    assert "trades -2000.00" in item.detail and "dividends +3.00" in item.detail
    assert report_status([item]) == "warn"


def test_cash_drift_is_material_only_when_configured():
    [item] = cash_drift(
        "settled_cash", "EUR", 1000.0, 900.0, CashFlows(), tolerance=1.0, material=True
    )
    assert (item.kind, item.key, item.material) == ("settled_cash", "EUR", True)
    assert "settled cash" in item.detail
    assert report_status([item]) == "drift"


def test_a_rise_the_ledger_does_not_explain_also_warns():
    [item] = cash_drift("cash", "USD", 1000.0, 1500.0, CashFlows(trades=400.0), tolerance=1.0)
    assert item.broker == pytest.approx(500.0) and item.ours == pytest.approx(400.0)
    assert "+100.00 unexplained" in item.detail


# ---- broker statement --------------------------------------------------------------------


def ex(exec_id="e1", qty=10.0, commission=1.0, ccy="USD", ref="r1"):
    return StatementExecution(
        exec_id=exec_id,
        quantity=qty,
        symbol="AAPL",
        order_ref=ref,
        trade_date=date(2026, 9, 28),
        commission=commission,
        commission_currency=ccy,
    )


def fill(exec_id="e1", qty=10.0, fee=1.0, ccy="USD"):
    return BookedFill(exec_id=exec_id, ticker="AAPL.US", quantity=qty, fee=fee, fee_currency=ccy)


def test_matching_statement_and_fills_are_clean():
    assert statement_drift([ex()], [fill()], commission_tolerance=0.01) == []


def test_an_execution_the_ledger_never_booked_is_material():
    [item] = statement_drift(
        [ex("e1"), ex("e2", qty=-4.0)], [fill("e1")], commission_tolerance=0.01
    )
    assert (item.kind, item.key, item.ours, item.broker) == (
        "statement_missing_execution",
        "e2",
        None,
        -4.0,
    )
    assert item.material and "AAPL" in item.detail


def test_a_booked_fill_the_statement_does_not_list_is_material():
    [item] = statement_drift(
        [ex("e1")], [fill("e1"), fill("e9", qty=3.0)], commission_tolerance=0.01
    )
    assert (item.kind, item.key, item.ours, item.broker) == (
        "statement_extra_execution",
        "e9",
        3.0,
        None,
    )
    assert item.material and "AAPL.US" in item.detail


def test_a_quantity_difference_on_one_execution_is_material():
    [item] = statement_drift([ex(qty=10.0)], [fill(qty=9.0)], commission_tolerance=0.01)
    assert (item.kind, item.ours, item.broker, item.material) == (
        "statement_quantity",
        9.0,
        10.0,
        True,
    )


def test_a_tiny_quantity_difference_is_noise():
    assert (
        statement_drift([ex(qty=10.0)], [fill(qty=10.0 + 1e-12)], commission_tolerance=0.01) == []
    )


def test_a_commission_difference_warns():
    [item] = statement_drift([ex(commission=1.35)], [fill(fee=1.0)], commission_tolerance=0.01)
    assert (item.kind, item.key, item.ours, item.broker) == (
        "statement_commission",
        "e1",
        1.0,
        1.35,
    )
    assert not item.material
    assert report_status([item]) == "warn"


def test_a_commission_difference_inside_the_tolerance_is_clean():
    assert statement_drift([ex(commission=1.01)], [fill(fee=1.0)], commission_tolerance=0.01) == []
    [item] = statement_drift([ex(commission=1.02)], [fill(fee=1.0)], commission_tolerance=0.01)
    assert item.kind == "statement_commission"


def test_a_commission_not_booked_yet_warns_with_the_statement_figure():
    [item] = statement_drift(
        [ex(commission=1.0)], [fill(fee=None, ccy=None)], commission_tolerance=0.01
    )
    assert (item.kind, item.ours, item.broker) == ("statement_commission", None, 1.0)


def test_commissions_in_different_currencies_are_not_compared():
    assert statement_drift([ex(commission=2.0, ccy="EUR")], [fill(fee=1.0, ccy="USD")],
                           commission_tolerance=0.01) == []  # fmt: skip


def test_a_statement_without_a_commission_is_not_compared():
    assert statement_drift([ex(commission=None)], [fill(fee=1.0)], commission_tolerance=0.01) == []


def test_statement_items_are_sorted_material_first():
    items = statement_drift(
        [ex("e1", commission=5.0), ex("e2")],
        [fill("e1"), fill("e3")],
        commission_tolerance=0.01,
    )
    assert [(i.kind, i.key) for i in items] == [
        ("statement_extra_execution", "e3"),
        ("statement_missing_execution", "e2"),
        ("statement_commission", "e1"),
    ]

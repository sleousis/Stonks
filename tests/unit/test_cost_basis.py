"""Average cost per position from a fill history (weighted-average method)."""

from __future__ import annotations

import pytest

from stonks.app.cost_basis import FillLot, average_costs


def test_buys_average_in_with_fees():
    costs = average_costs(
        [
            FillLot("A.US", 10, 100.0, fee=1.0),
            FillLot("A.US", 10, 110.0, fee=1.0),
        ]
    )
    assert costs["A.US"] == pytest.approx((1000 + 1100 + 2) / 20)


def test_sells_keep_the_average_and_a_flat_position_resets_it():
    costs = average_costs(
        [
            FillLot("A.US", 10, 100.0),
            FillLot("A.US", -4, 150.0),
        ]
    )
    assert costs["A.US"] == pytest.approx(100.0)
    costs = average_costs(
        [
            FillLot("A.US", 10, 100.0),
            FillLot("A.US", -10, 150.0),
            FillLot("A.US", 5, 80.0),
        ]
    )
    assert costs["A.US"] == pytest.approx(80.0)


def test_closed_positions_have_no_cost():
    assert "A.US" not in average_costs([FillLot("A.US", 3, 10.0), FillLot("A.US", -3, 12.0)])


def test_crossing_zero_starts_a_new_basis_at_the_crossing_price():
    costs = average_costs([FillLot("A.US", 5, 10.0), FillLot("A.US", -8, 12.0)])
    assert costs["A.US"] == pytest.approx(12.0)


def test_tickers_are_independent():
    costs = average_costs([FillLot("A.US", 1, 10.0), FillLot("B.US", 2, 20.0)])
    assert costs == {"A.US": pytest.approx(10.0), "B.US": pytest.approx(20.0)}

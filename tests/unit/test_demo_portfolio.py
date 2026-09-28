"""The demo portfolio (roadmap 23.17): sample data a new person can open,
built from synthetic bars, never written to real books or the lake."""

from __future__ import annotations

from datetime import date

import pytest

from stonks.accounts.demo import DEMO_INSTRUMENTS, build_demo


def test_the_demo_is_labelled_and_deterministic():
    one = build_demo(seed=7, as_of=date(2026, 9, 28))
    two = build_demo(seed=7, as_of=date(2026, 9, 28))
    assert one == two
    assert one.label == "Sample data"
    assert "demo" in one.name.lower()
    assert {p.ticker for p in one.positions} <= {i.ticker for i in DEMO_INSTRUMENTS}
    assert all(p.ticker.endswith(".DEMO") for p in one.positions)


def test_the_figures_add_up():
    demo = build_demo(seed=3, as_of=date(2026, 9, 28))
    invested = sum(p.value for p in demo.positions)
    assert demo.total_value == pytest.approx(demo.cash + invested)
    assert sum(p.weight for p in demo.positions) + demo.cash / demo.total_value == pytest.approx(1)
    for p in demo.positions:
        assert p.value == pytest.approx(p.quantity * p.price)
        assert p.pnl == pytest.approx((p.price - p.cost) * p.quantity)
    assert demo.curve[-1].value == pytest.approx(demo.total_value)
    assert demo.curve[-1].day == date(2026, 9, 28)
    sunday = build_demo(seed=3, as_of=date(2026, 9, 27))
    assert sunday.curve[-1].day == date(2026, 9, 25)  # the last weekday before it
    assert demo.total_return == pytest.approx(demo.total_value / demo.start_value - 1)


def test_another_seed_gives_other_numbers():
    a = build_demo(seed=1, as_of=date(2026, 9, 28))
    b = build_demo(seed=2, as_of=date(2026, 9, 28))
    assert a.total_value != b.total_value

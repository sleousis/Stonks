"""Phase 16.1: the ``BorrowSource`` and ``MarginModel`` seams, hand-checked."""

from __future__ import annotations

from datetime import date

import pytest

from stonks.core.types import Portfolio
from stonks.execution.borrow import (
    BorrowQuote,
    BorrowSettings,
    FlatBorrow,
    daily_fee,
)
from stonks.execution.margin import (
    CashMargin,
    MarginRates,
    MarginSettings,
    RegTMargin,
    get_margin_model,
)

D = date(2026, 3, 20)


# ---- borrow ------------------------------------------------------------------------


def test_flat_borrow_defaults_to_general_collateral_per_asset_class() -> None:
    src = FlatBorrow()
    assert src.quote("X", D) == BorrowQuote("easy", 0.005)
    assert src.quote("BTC", D, "crypto") == BorrowQuote("easy", 0.05)
    assert not FlatBorrow.has_history


def test_flat_borrow_hard_none_and_override() -> None:
    src = BorrowSettings(hard=("GME",), none=("TINY",), hard_fee_rate_annual=0.3).build()
    assert src.quote("GME", D) == BorrowQuote("hard", 0.3)
    none = src.quote("TINY", D)
    assert none is not None and not none.shortable
    special = FlatBorrow(overrides={"X": BorrowQuote("hard", 0.2, available_shares=500)})
    assert special.quote("X", D) == BorrowQuote("hard", 0.2, 500)


def test_flat_borrow_unknown_class_has_zero_fee() -> None:
    src = BorrowSettings(fee_rate_annual={"equity": 0.01}).build()
    assert src.quote("B", D, "bond") == BorrowQuote("easy", 0.0)


def test_daily_fee_by_hand() -> None:
    # 100 shares short at 50, 3.6 %/yr: 5000 x 0.036 / 360 = 0.50 a day.
    q = BorrowQuote("easy", 0.036)
    assert daily_fee(-100, 50.0, q) == pytest.approx(0.5)
    assert daily_fee(-100, 50.0, q, days=3) == pytest.approx(1.5)


# ---- margin -------------------------------------------------------------------------


def test_cash_margin_is_todays_account() -> None:
    m = MarginSettings().build()
    assert isinstance(m, CashMargin) and not m.allows_short
    p = Portfolio(cash=1_000.0, positions={"X": 10.0})
    assert m.excess_equity(p, {"X": 50.0}) == 1_000.0
    assert m.maintenance_requirement(p, {"X": 50.0}) == 0.0
    assert m.deficit(p, {"X": 1.0}) == 0.0
    assert m.initial_requirement("X", 10, 50.0) == 500.0
    assert m.cover_quantity(10, 50.0, 0.0) == 0.0
    assert m.cover_quantity(10, 50.0, 5.0) == 10.0  # no rate: close it all


def test_reg_t_short_by_hand() -> None:
    # Start with 10,000 cash, short 100 at 50: cash 15,000, equity 10,000.
    m = RegTMargin()
    p = Portfolio(cash=15_000.0, positions={"X": -100.0})
    at50 = {"X": 50.0}
    assert m.initial_requirement("X", -100, 50.0) == 2_500.0
    assert m.excess_equity(p, at50) == 7_500.0
    assert m.maintenance_requirement(p, at50) == pytest.approx(1_500.0)
    assert m.deficit(p, at50) == 0.0
    # At 120: equity 3,000, maintenance 0.3 x 12,000 = 3,600, deficit 600.
    at120 = {"X": 120.0}
    assert m.deficit(p, at120) == pytest.approx(600.0)
    # Covering q frees 0.3 x 120 x q = 36 q: q = 600 / 36.
    assert m.cover_quantity(-100, 120.0, 600.0) == pytest.approx(600.0 / 36.0)
    assert m.cover_quantity(-100, 120.0, 1e9) == 100.0


def test_reg_t_long_rates_and_overrides() -> None:
    m = MarginSettings(
        model="reg_t",
        overrides={"crypto": MarginRates(initial_short=1.5, maintenance_short=1.0)},
        debit_rate_annual=0.07,
    ).build()
    assert m.debit_rate_annual == 0.07
    assert m.initial_requirement("X", 10, 10.0) == 50.0
    assert m.initial_requirement("B", -10, 10.0, "crypto") == 150.0
    p = Portfolio(cash=0.0, positions={"X": 10.0, "B": -1.0})
    assert m.maintenance_requirement(p, {"X": 10.0, "B": 10.0}, {"B": "crypto"}) == pytest.approx(
        25.0 + 10.0
    )
    # Unpriced positions count as 0.
    assert m.maintenance_requirement(p, {"X": float("nan")}) == 0.0


def test_unknown_model_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown margin model"):
        get_margin_model("portfolio")

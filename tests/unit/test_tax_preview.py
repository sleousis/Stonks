"""Pre-trade tax preview and the tax owed this year (roadmap 23.5)."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from stonks.tax import TaxFill, TaxSettings
from stonks.tax.preview import preview_trade, year_tax
from stonks.tax.settings import TaxRates, TaxRatesConfig


def _fill(i: int, side: str, qty: float, price: float, day: str, fee: float = 0.0) -> TaxFill:
    return TaxFill(
        id=i,
        ticker="UP.US",
        side=side,  # type: ignore[arg-type]
        quantity=qty,
        price=price,
        fee=fee,
        filled_at=datetime.fromisoformat(f"{day}T15:00:00+00:00"),
        currency="USD",
    )


RATES = TaxRatesConfig(us=TaxRates(short_term=0.30, long_term=0.15))
WHEN = datetime(2026, 4, 2, 15, 0, tzinfo=UTC)


def test_sell_closes_fifo_lots_with_holding_periods_and_tax():
    fills = [_fill(1, "buy", 10, 50.0, "2024-01-10"), _fill(2, "buy", 10, 80.0, "2026-01-05")]
    p = preview_trade(fills, TaxSettings(), RATES, ticker="UP.US", side="sell", quantity=15,
                      price=100.0, when=WHEN)  # fmt: skip
    assert [(lot.open_fill_id, lot.quantity) for lot in p.lots] == [(1, 10), (2, 5)]
    assert [lot.holding_period for lot in p.lots] == ["long", "short"]
    assert p.long_term_gain == pytest.approx(500.0)
    assert p.short_term_gain == pytest.approx(100.0)
    assert p.realized_gain == pytest.approx(600.0)
    assert p.estimated_tax == pytest.approx(500 * 0.15 + 100 * 0.30)
    assert p.proceeds == pytest.approx(1500.0)
    assert p.after_tax_proceeds == pytest.approx(1500.0 - p.estimated_tax)
    assert p.wash_sale_warning is None


def test_specific_picks_choose_the_lots():
    fills = [_fill(1, "buy", 10, 50.0, "2024-01-10"), _fill(2, "buy", 10, 80.0, "2026-01-05")]
    p = preview_trade(fills, TaxSettings(lot_method="specific"), RATES, ticker="UP.US",
                      side="sell", quantity=5, price=100.0, when=WHEN, picks=[(2, 5.0)])  # fmt: skip
    assert [lot.open_fill_id for lot in p.lots] == [2]


def test_a_loss_with_a_recent_buy_warns_of_a_wash_sale():
    fills = [_fill(1, "buy", 10, 120.0, "2025-06-01"), _fill(2, "buy", 5, 101.0, "2026-03-20")]
    p = preview_trade(fills, TaxSettings(), RATES, ticker="UP.US", side="sell", quantity=10,
                      price=100.0, when=WHEN)  # fmt: skip
    assert p.realized_gain < 0 and p.wash_sale_disallowed > 0
    assert p.wash_sale_warning and "30 days" in p.wash_sale_warning
    assert p.estimated_tax == 0.0


def test_a_loss_warns_about_buying_back():
    fills = [_fill(1, "buy", 10, 120.0, "2025-06-01")]
    p = preview_trade(fills, TaxSettings(), RATES, ticker="UP.US", side="sell", quantity=10,
                      price=100.0, when=WHEN)  # fmt: skip
    assert p.wash_sale_disallowed == 0.0
    assert p.wash_sale_warning and "buy" in p.wash_sale_warning


def test_a_buy_after_a_recent_loss_warns():
    fills = [_fill(1, "buy", 10, 120.0, "2025-06-01"), _fill(2, "sell", 10, 100.0, "2026-03-25")]
    p = preview_trade(fills, TaxSettings(), RATES, ticker="UP.US", side="buy", quantity=10,
                      price=99.0, when=WHEN)  # fmt: skip
    assert p.lots == () and p.realized_gain == 0.0
    assert p.wash_sale_warning and "disallow" in p.wash_sale_warning


def test_no_wash_warning_outside_the_us():
    fills = [_fill(1, "buy", 10, 120.0, "2025-06-01"), _fill(2, "buy", 5, 101.0, "2026-03-20")]
    p = preview_trade(fills, TaxSettings(jurisdiction="uk"), RATES, ticker="UP.US",
                      side="sell", quantity=10, price=100.0, when=WHEN)  # fmt: skip
    assert p.wash_sale_warning is None


def test_year_tax_nets_losses_within_each_term():
    fills = [
        _fill(1, "buy", 10, 50.0, "2024-01-10"),
        _fill(2, "sell", 10, 100.0, "2026-02-01"),  # long +500
        _fill(3, "buy", 10, 100.0, "2026-02-10"),
        _fill(4, "sell", 10, 90.0, "2026-03-20"),  # short -100
        _fill(5, "buy", 10, 90.0, "2026-03-21"),
        _fill(6, "sell", 10, 120.0, "2026-03-25"),  # short +300 (wash basis moves here)
    ]
    y = year_tax(fills, TaxSettings(wash_sales=False), RATES, year=2026)
    assert y.long_term_gain == pytest.approx(500.0)
    assert y.short_term_gain == pytest.approx(200.0)
    assert y.estimated_tax == pytest.approx(500 * 0.15 + 200 * 0.30)
    assert y.disposals == 3
    assert year_tax(fills, TaxSettings(), RATES, year=2025).estimated_tax == 0.0


def test_rates_per_jurisdiction_have_defaults():
    cfg = TaxRatesConfig()
    assert cfg.for_jurisdiction("uk").long_term > 0
    with pytest.raises(ValueError):
        TaxRates(short_term=1.5, long_term=0.1)


def test_year_tax_carries_a_net_loss_to_the_other_term():
    fills = [
        _fill(1, "buy", 10, 50.0, "2024-01-10"),
        _fill(2, "sell", 10, 100.0, "2026-02-01"),  # long +500
        _fill(3, "buy", 10, 100.0, "2026-02-10"),
        _fill(4, "sell", 10, 70.0, "2026-03-20"),  # short -300
    ]
    y = year_tax(fills, TaxSettings(wash_sales=False), RATES, year=2026, as_of=date(2026, 12, 31))
    assert y.estimated_tax == pytest.approx(200 * 0.15)

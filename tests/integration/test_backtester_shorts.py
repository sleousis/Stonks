"""Phase 16.1: short books end to end through the backtest engine.

Every number is worked out by hand in the comments. Days are business days
from Monday 2024-06-03; orders decided at a bar fill at the next bar's open.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from stonks.backtest.engine import BacktestConfig, Backtester
from stonks.backtest.simulated_broker import SimulatedBroker
from stonks.backtest.trades import build_round_trips
from stonks.core.types import Order, Portfolio
from stonks.execution.borrow import BorrowQuote, FlatBorrow
from stonks.execution.margin import RegTMargin
from stonks.store.lake import DuckDBLake

DAYS = [d.date() for d in pd.bdate_range("2024-06-03", periods=8)]
FREE = FlatBorrow(overrides={"X.US": BorrowQuote("easy", 0.0)})


class Scripted:
    """Trades a fixed script: ``{decision day: (side, quantity)}``."""

    id = "scripted"
    supports_short = True

    def __init__(self, script: dict[date, tuple[str, float]]) -> None:
        self.script = script

    def estimate_return(self, ticker, as_of, lake):
        return 1.0

    def decide(self, my_picks, portfolio, prices, as_of):
        step = self.script.get(as_of.date())
        if step is None:
            return []
        side, qty = step
        return [Order(f"{as_of.date()}:X.US:{side}", "X.US", side, qty)]  # type: ignore[arg-type]


class LongOnlyScripted(Scripted):
    supports_short = False


def _lake(tmp_path, closes, opens=None) -> DuckDBLake:
    lake = DuckDBLake(tmp_path / "lake.duckdb")
    lake.migrate()
    closes = np.asarray(closes, dtype=float)
    opens = closes if opens is None else np.asarray(opens, dtype=float)
    lake.upsert_prices(
        pd.DataFrame(
            {
                "ticker": "X.US",
                "date": DAYS[: len(closes)],
                "open": opens,
                "high": np.maximum(opens, closes),
                "low": np.minimum(opens, closes),
                "close": closes,
                "adj_close": closes,
                "volume": 1_000_000.0,
            }
        )
    )
    return lake


def _run(lake, strategy, *, cash=10_000.0, borrow=FREE, **config):
    broker = SimulatedBroker(
        Portfolio(cash=cash), margin=RegTMargin(), borrow=borrow, allow_short=True
    )
    engine = Backtester(
        [strategy],
        broker,
        lake,
        BacktestConfig(start=DAYS[0], end=DAYS[-1], universe=["X.US"], allow_short=True, **config),
    )
    return engine.run(), broker, engine


def test_short_then_cover_pnl_by_hand(tmp_path) -> None:
    # Short 10 decided Mon, filled Tue open 100. Cover decided Thu, filled
    # Fri open 80. P&L = 10 x (100 - 80) = 200; final equity 10,200.
    opens = [100.0, 100.0, 95.0, 90.0, 80.0, 80.0, 80.0, 80.0]
    lake = _lake(tmp_path, closes=opens, opens=opens)
    report, broker, _ = _run(lake, Scripted({DAYS[0]: ("sell", 10.0), DAYS[3]: ("buy", 10.0)}))
    assert broker.fetch_portfolio().positions == {}
    assert report.equity_curve[-1] == pytest.approx(10_200.0)
    # Marked as a liability while open: Wed close 95 -> 10,000 + 1,000 - 950.
    assert report.equity_curve[2] == pytest.approx(10_050.0)
    [trip] = build_round_trips(broker.fills)
    assert trip.side == "short" and trip.pnl == pytest.approx(200.0)
    assert [f.order_client_id.rsplit(":", 1)[1] for f in broker.fills] == ["short", "cover"]


def test_borrow_fee_accrues_each_night_by_hand(tmp_path) -> None:
    # Short 10 at 50 from Tue open to Fri open; 3.6 %/yr is 500 x 0.036 /
    # 360 = 0.05 a night. Charged at the Tue, Wed and Thu closes: 0.15.
    lake = _lake(tmp_path, closes=[50.0] * 8)
    borrow = FlatBorrow(overrides={"X.US": BorrowQuote("easy", 0.036)})
    report, broker, _ = _run(
        lake, Scripted({DAYS[0]: ("sell", 10.0), DAYS[3]: ("buy", 10.0)}), borrow=borrow
    )
    assert [e.amount for e in broker.financing] == pytest.approx([-0.05] * 3)
    assert report.equity_curve[-1] == pytest.approx(10_000.0 - 0.15)


def test_a_dividend_on_a_short_is_paid_in_full(tmp_path) -> None:
    # Short 10 filled Tue; 2.00 dividend goes ex Thu with 30 % withholding:
    # the short still pays 20.00.
    lake = _lake(tmp_path, closes=[50.0] * 8)
    lake.upsert_dividends(
        pd.DataFrame(
            [
                {
                    "ticker": "X.US",
                    "ex_date": DAYS[3],
                    "amount": 2.0,
                    "currency": None,
                    "pay_date": None,
                    "record_date": None,
                    "declaration_date": None,
                }
            ]
        )
    )
    report, broker, _ = _run(
        lake, Scripted({DAYS[0]: ("sell", 10.0)}), dividend_withholding_rate=0.3
    )
    [record] = report.corporate_actions
    assert record.cash_delta == pytest.approx(-20.0)
    assert report.equity_curve[-1] == pytest.approx(10_000.0 - 20.0)


def test_a_split_on_a_short_keeps_its_value(tmp_path) -> None:
    # Short 10 at 100; a 2:1 split on Thu makes it short 20 at 50.
    closes = [100.0] * 3 + [50.0] * 5
    lake = _lake(tmp_path, closes=closes)
    lake.upsert_stock_splits(pd.DataFrame([{"ticker": "X.US", "date": DAYS[3], "ratio": 2.0}]))
    report, broker, _ = _run(lake, Scripted({DAYS[0]: ("sell", 10.0)}))
    assert broker.fetch_portfolio().positions == {"X.US": -20.0}
    assert report.equity_curve[-1] == pytest.approx(10_000.0)
    assert broker.average_cost("X.US") == pytest.approx(50.0)


def test_a_margin_breach_forces_a_cover_by_hand(tmp_path) -> None:
    # Cash 1,000, short 10 filled Tue at 50: cash 1,500. At Wed's close of
    # 130 equity is 1,500 - 1,300 = 200, maintenance 0.3 x 1,300 = 390,
    # deficit 190. A cover frees 0.3 x 130 = 39 a share: 190 / 39 shares,
    # queued at Wed and filled at Thu's open.
    closes = [50.0, 50.0, 130.0, 130.0, 130.0, 130.0, 130.0, 130.0]
    lake = _lake(tmp_path, closes=closes)
    _, broker, engine = _run(lake, Scripted({DAYS[0]: ("sell", 10.0)}), cash=1_000.0)
    forced = engine.forced_orders[0]
    assert forced.client_id.startswith("margin:") and forced.client_id.endswith(":cover")
    assert (forced.side, forced.position_effect) == ("buy", "close")
    assert forced.quantity == pytest.approx(190.0 / 39.0)
    assert broker.fetch_portfolio().positions["X.US"] == pytest.approx(-10.0 + 190.0 / 39.0)
    assert broker.margin_deficit() == pytest.approx(0.0, abs=1e-6)


def test_a_long_only_strategy_cannot_open_a_short_in_a_short_book(tmp_path) -> None:
    lake = _lake(tmp_path, closes=[50.0] * 8)
    _, broker, _ = _run(lake, LongOnlyScripted({DAYS[0]: ("sell", 10.0)}))
    assert broker.fills == ()


def test_both_switches_must_agree(tmp_path) -> None:
    lake = _lake(tmp_path, closes=[50.0] * 8)
    config = BacktestConfig(start=DAYS[0], end=DAYS[-1], universe=["X.US"], allow_short=True)
    with pytest.raises(ValueError, match="must agree"):
        Backtester([Scripted({})], SimulatedBroker(Portfolio(cash=1.0)), lake, config).run()


def test_a_crossing_order_is_split_when_it_fills(tmp_path) -> None:
    # Buy 5 (Tue), then sell 15 decided Wed: fills Thu as sell 5 + short 10.
    lake = _lake(tmp_path, closes=[20.0] * 8)
    _, broker, _ = _run(lake, Scripted({DAYS[0]: ("buy", 5.0), DAYS[2]: ("sell", 15.0)}))
    assert [(f.side, f.quantity) for f in broker.fills] == [
        ("buy", 5.0),
        ("sell", 5.0),
        ("sell", 10.0),
    ]
    assert broker.fetch_portfolio().positions == {"X.US": -10.0}

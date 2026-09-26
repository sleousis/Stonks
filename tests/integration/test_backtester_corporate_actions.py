"""Backtest accounting for splits and cash dividends.

The engine trades and marks on raw prices; on each ex-date it multiplies
held quantity by the split ratio (value unchanged) and credits
``quantity x dividend`` as cash, recording each event in the report."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime

import numpy as np
import pandas as pd
import pytest

from stonks.backtest.corporate_actions import CorporateActionRecord
from stonks.backtest.engine import BacktestConfig, Backtester
from stonks.backtest.simulated_broker import SimulatedBroker
from stonks.core.corporate_actions import CorporateActions, Dividend, Split
from stonks.core.types import Order, Portfolio
from stonks.store.lake import DuckDBLake
from stonks.strategies.base import BaseStrategy
from stonks.strategies.examples.buy_and_hold import BuyAndHold
from stonks.strategies.examples.momentum import Momentum

DAYS = [d.date() for d in pd.bdate_range("2024-06-03", periods=10)]
EX = DAYS[5]


def _lake(tmp_path, closes, opens=None, ticker="X.US", days=DAYS) -> DuckDBLake:
    lake = DuckDBLake(tmp_path / "lake.duckdb")
    lake.migrate()
    closes = np.asarray(closes, dtype=float)
    opens = closes if opens is None else np.asarray(opens, dtype=float)
    lake.upsert_prices(
        pd.DataFrame(
            {
                "ticker": ticker,
                "date": days[: len(closes)],
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


def _split(lake, ratio=10.0, day=EX, ticker="X.US"):
    lake.upsert_stock_splits(pd.DataFrame([{"ticker": ticker, "date": day, "ratio": ratio}]))


def _dividend(lake, amount, day=EX, ticker="X.US"):
    lake.upsert_dividends(
        pd.DataFrame(
            [
                {
                    "ticker": ticker,
                    "ex_date": day,
                    "amount": amount,
                    "currency": None,
                    "pay_date": None,
                    "record_date": None,
                    "declaration_date": None,
                }
            ]
        )
    )


def _run(lake, strategies, *, cash=10_000.0, start=DAYS[0], end=DAYS[-1], **config):
    broker = SimulatedBroker(Portfolio(cash=cash))
    report = Backtester(
        strategies,
        broker,
        lake,
        BacktestConfig(start=start, end=end, universe=["X.US"], **config),
    ).run()
    return report, broker


def _hold():
    return BuyAndHold({"ticker": "X.US", "allocation": 1.0})


SPLIT_CLOSES = [1000.0] * 5 + [100.0] * 5


def test_split_on_held_position_keeps_equity_flat(tmp_path):
    lake = _lake(tmp_path, SPLIT_CLOSES)
    _split(lake)
    report, broker = _run(lake, [_hold()])
    assert report.equity_curve == pytest.approx([10_000.0] * 10)
    assert broker.fetch_portfolio().positions["X.US"] == pytest.approx(100.0)
    assert report.corporate_actions == (
        CorporateActionRecord(
            timestamp=datetime(2024, 6, 10),
            ticker="X.US",
            kind="split",
            value=10.0,
            quantity_before=pytest.approx(10.0),
            quantity_after=pytest.approx(100.0),
            cash_delta=0.0,
        ),
    )


def test_without_corporate_action_data_the_raw_drop_is_unchanged(tmp_path):
    lake = _lake(tmp_path, SPLIT_CLOSES)
    report, broker = _run(lake, [_hold()])
    assert report.equity_curve[-1] == pytest.approx(1_000.0)
    assert report.corporate_actions == ()


def test_dividend_credits_quantity_times_amount_as_cash(tmp_path):
    lake = _lake(tmp_path, [100.0] * 5 + [98.0] * 5)
    _dividend(lake, 2.0)
    report, broker = _run(lake, [_hold()])
    portfolio = broker.fetch_portfolio()
    assert portfolio.positions["X.US"] == pytest.approx(100.0)
    assert portfolio.cash == pytest.approx(200.0)
    assert report.equity_curve == pytest.approx([10_000.0] * 10)
    (record,) = report.corporate_actions
    assert (record.kind, record.value, record.cash_delta) == ("dividend", 2.0, pytest.approx(200.0))
    assert record.quantity_before == record.quantity_after == pytest.approx(100.0)


def test_dividend_withholding_rate(tmp_path):
    lake = _lake(tmp_path, [100.0] * 10)
    _dividend(lake, 2.0)
    _report, broker = _run(lake, [_hold()], dividend_withholding_rate=0.15)
    assert broker.fetch_portfolio().cash == pytest.approx(170.0)


def test_withholding_rate_must_be_a_fraction():
    with pytest.raises(ValueError):
        BacktestConfig(
            start=DAYS[0], end=DAYS[-1], universe=["X.US"], dividend_withholding_rate=1.5
        )


def test_dividend_is_not_credited_to_a_buy_filled_on_the_ex_date(tmp_path):
    # decide on the bar before the ex-date, fill at the ex-date open
    lake = _lake(tmp_path, [100.0] * 10)
    _dividend(lake, 2.0)
    report, broker = _run(lake, [_hold()], start=DAYS[4])
    assert broker.fetch_portfolio().cash == pytest.approx(0.0)
    assert report.corporate_actions == ()


def test_buy_queued_before_a_split_is_scaled_by_the_ratio(tmp_path):
    # decided at 1000 on the bar before the ex-date, fills at the 100 open
    lake = _lake(tmp_path, SPLIT_CLOSES)
    _split(lake)
    report, broker = _run(lake, [_hold()], start=DAYS[4])
    portfolio = broker.fetch_portfolio()
    assert portfolio.positions["X.US"] == pytest.approx(100.0)
    assert portfolio.cash == pytest.approx(0.0)
    assert report.corporate_actions == ()  # nothing held on the ex-date


class _SellOn(BaseStrategy):
    """Buys on the first bar; sells everything on ``sell_on``."""

    id = "sell_on"

    @classmethod
    def parameter_spec(cls):
        return []

    def __init__(self, sell_on: date) -> None:
        super().__init__({})
        self._sell_on = sell_on

    def estimate_return(self, ticker, as_of, lake):
        return 1.0

    def decide(
        self,
        my_picks: Sequence[tuple[float, str]],
        portfolio: Portfolio,
        prices: Mapping[str, float],
        as_of,
    ) -> list[Order]:
        held = portfolio.positions.get("X.US", 0.0)
        day = as_of.date() if isinstance(as_of, datetime) else as_of
        if day == self._sell_on and held > 0:
            return [Order(client_id=f"sell:{day}", ticker="X.US", side="sell", quantity=held)]
        if held <= 0 and portfolio.cash > 0 and day < self._sell_on:
            qty = portfolio.cash / prices["X.US"]
            return [Order(client_id=f"buy:{day}", ticker="X.US", side="buy", quantity=qty)]
        return []


def test_sell_queued_before_a_split_sells_the_whole_split_position(tmp_path):
    lake = _lake(tmp_path, SPLIT_CLOSES)
    _split(lake)
    report, broker = _run(lake, [_SellOn(DAYS[4])])
    portfolio = broker.fetch_portfolio()
    assert portfolio.positions.get("X.US", 0.0) == pytest.approx(0.0)
    assert portfolio.cash == pytest.approx(10_000.0)
    sell = broker.reconcile()[-1]
    assert (sell.side, sell.quantity, sell.price) == ("sell", pytest.approx(100.0), 100.0)


def test_reverse_split(tmp_path):
    lake = _lake(tmp_path, [10.0] * 5 + [100.0] * 5)
    _split(lake, ratio=0.1)
    report, broker = _run(lake, [_hold()])
    assert broker.fetch_portfolio().positions["X.US"] == pytest.approx(100.0)
    assert report.equity_curve == pytest.approx([10_000.0] * 10)


def test_momentum_holds_through_a_split(tmp_path):
    days = [d.date() for d in pd.bdate_range("2024-06-03", periods=20)]
    closes = np.linspace(1000.0, 1019.0, 20)
    closes[12:] /= 10.0
    lake = _lake(tmp_path, closes, days=days)
    _split(lake, day=days[12])
    strat = Momentum({"lookback_days": 5, "threshold": 0.0, "allocation": 1.0})
    report, broker = _run(lake, [strat], end=days[-1])
    assert [f.side for f in broker.reconcile()] == ["buy"]  # no false exit
    assert min(np.diff(report.equity_curve)) > -1e-9


def test_corporate_actions_load_once_from_the_provider(tmp_path):
    lake = _lake(tmp_path, SPLIT_CLOSES)
    calls: list[list[str]] = []

    class Provider:
        def load(self, tickers):
            calls.append(list(tickers))
            return CorporateActions.from_events(
                [Split("X.US", EX, 10.0), Dividend("X.US", EX, 1.0)]
            )

    broker = SimulatedBroker(Portfolio(cash=10_000.0))
    report = Backtester(
        [_hold()],
        broker,
        lake,
        BacktestConfig(start=DAYS[0], end=DAYS[-1], universe=["X.US"]),
        corporate_actions=Provider(),
    ).run()
    assert calls == [["X.US"]]
    # split first, then the dividend on the post-split share count
    assert [r.kind for r in report.corporate_actions] == ["split", "dividend"]
    assert broker.fetch_portfolio().cash == pytest.approx(100.0)

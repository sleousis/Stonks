"""Backtester annualizes Sharpe on the universe's trading calendar, resolved
from the lake's ``instruments.asset_class`` (unknown tickers are equity)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from stonks.backtest.engine import BacktestConfig, Backtester
from stonks.backtest.report import compute_report, periods_per_year
from stonks.backtest.simulated_broker import SimulatedBroker
from stonks.core.interval import Interval
from stonks.core.types import Portfolio
from stonks.store.lake import DuckDBLake
from stonks.strategies.examples.buy_and_hold import BuyAndHold

_T0 = datetime(2026, 4, 1, tzinfo=UTC)
_CLOSES = [100.0, 101.0, 100.5, 102.0, 101.0, 103.0, 102.5]


def _rows(ticker: str) -> list[dict]:
    return [
        {
            "ticker": ticker,
            "timestamp": _T0 + timedelta(hours=i),
            "open": c,
            "high": c,
            "low": c,
            "close": c,
            "adj_close": c,
            "volume": 1_000,
        }
        for i, c in enumerate(_CLOSES)
    ]


def _lake(tmp_path, tickers: list[str], asset_classes: dict[str, str]) -> DuckDBLake:
    lake = DuckDBLake(tmp_path / "lake.duckdb")
    lake.migrate()
    rows = [r for t in tickers for r in _rows(t)]
    lake.upsert_bars(pd.DataFrame(rows), interval=Interval.HOUR_1)
    for ticker, asset_class in asset_classes.items():
        lake.con.execute(
            "INSERT INTO instruments (id, asset_class) VALUES (?, ?)", [ticker, asset_class]
        )
    return lake


def _run(lake: DuckDBLake, ticker: str, universe: list[str]):
    broker = SimulatedBroker(Portfolio(cash=10_000.0, positions={}))
    config = BacktestConfig(
        start=_T0,
        end=_T0 + timedelta(days=1),
        universe=universe,
        interval=Interval.HOUR_1,
    )
    strategy = BuyAndHold({"ticker": ticker, "allocation": 1.0})
    return Backtester([strategy], broker, lake, config).run()


def _expected_sharpe(report, asset_classes: list[str]) -> float:
    return compute_report(
        report.strategy_id,
        report.equity_dates,
        report.equity_curve,
        periods_per_year=periods_per_year(Interval.HOUR_1, asset_classes),
    ).sharpe


def test_crypto_universe_annualizes_on_the_24_7_calendar(tmp_path):
    lake = _lake(tmp_path, ["BTC-USD.CC"], {"BTC-USD.CC": "crypto"})
    report = _run(lake, "BTC-USD.CC", ["BTC-USD.CC"])
    assert report.sharpe != 0.0
    assert report.sharpe == pytest.approx(_expected_sharpe(report, ["crypto"]))
    assert report.sharpe != pytest.approx(_expected_sharpe(report, ["equity"]))
    lake.close()


def test_ticker_without_instruments_row_is_treated_as_equity(tmp_path):
    lake = _lake(tmp_path, ["AAPL.US"], {})
    report = _run(lake, "AAPL.US", ["AAPL.US"])
    assert report.sharpe == pytest.approx(_expected_sharpe(report, ["equity"]))
    lake.close()


def test_mixed_universe_uses_the_densest_calendar(tmp_path):
    lake = _lake(
        tmp_path,
        ["AAPL.US", "BTC-USD.CC"],
        {"AAPL.US": "equity", "BTC-USD.CC": "crypto"},
    )
    report = _run(lake, "AAPL.US", ["AAPL.US", "BTC-USD.CC"])
    assert report.sharpe == pytest.approx(_expected_sharpe(report, ["crypto"]))
    lake.close()


def test_universe_ticker_without_bars_does_not_change_the_calendar(tmp_path):
    # A crypto ticker listed in the universe but with no bars in the window
    # contributes no equity-curve points, so the calendar stays equity.
    lake = _lake(tmp_path, ["AAPL.US"], {"BTC-USD.CC": "crypto"})
    report = _run(lake, "AAPL.US", ["AAPL.US", "BTC-USD.CC"])
    assert report.sharpe == pytest.approx(_expected_sharpe(report, ["equity"]))
    lake.close()


def test_empty_window_reports_zeroes(tmp_path):
    lake = _lake(tmp_path, [], {})
    report = _run(lake, "AAPL.US", ["AAPL.US"])
    assert report.equity_curve == []
    assert report.sharpe == 0.0
    lake.close()

"""DualMomentum: Antonacci's Global Equities Momentum on four ETFs.

Canned daily bars in an in-memory lake. The pick is taken at the latest
month end a decision can see (P12, RS-03), so it holds between month ends
and never reads a bar after the decision.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from stonks.core.types import Portfolio
from stonks.lab.catalog import strategy_catalog
from stonks.store.lake import DuckDBLake
from stonks.strategies.base import strategy_data_tickers, strategy_metadata
from stonks.strategies.examples.dual_momentum import DualMomentum

DATES = pd.bdate_range("2022-01-03", "2024-06-28")
TICKERS = ("SPY.US", "EFA.US", "BIL.US", "AGG.US")


def _closes(daily: dict[str, float], *, after: tuple[date, dict[str, float]] | None = None):
    """Closes from a constant daily log return per ticker, switching to the
    ``after`` rates from that day on."""
    out = {}
    for ticker in TICKERS:
        rates = np.full(len(DATES), daily.get(ticker, 0.0))
        if after is not None:
            start, later = after
            rates[DATES.date >= start] = later.get(ticker, 0.0)
        out[ticker] = 100.0 * np.exp(np.cumsum(rates))
    return out


def _lake(closes: dict[str, np.ndarray], skip: tuple[str, ...] = ()) -> DuckDBLake:
    frames = [
        pd.DataFrame(
            {
                "ticker": ticker,
                "date": DATES.date,
                "open": values,
                "high": values * 1.001,
                "low": values * 0.999,
                "close": values,
                "adj_close": values,
                "volume": 1_000_000.0,
            }
        )
        for ticker, values in closes.items()
        if ticker not in skip
    ]
    lake = DuckDBLake(Path(":memory:"))
    lake.migrate()
    lake.upsert_prices(pd.concat(frames, ignore_index=True))
    return lake


def _held(strategy: DualMomentum, lake: DuckDBLake, as_of) -> set[str]:
    return {t for t in TICKERS if strategy.estimate_return(t, as_of, lake) is not None}


MONTH_END = datetime(2024, 2, 29)  # a last session of the month
MID_MONTH = datetime(2024, 3, 14)


def test_catalogued_with_plain_metadata():
    assert strategy_catalog()["dual_momentum"] is DualMomentum
    strategy = DualMomentum({})
    meta = strategy_metadata(strategy)
    assert meta.alpha_family == "trend"
    assert set(meta.applicable_asset_classes) == {"equity", "bond"}
    assert strategy.title == "Dual momentum"
    assert strategy.required_history_bars >= 12 * 21 + 1
    assert set(strategy_data_tickers(strategy)) == set(TICKERS)


def test_lookback_is_tunable():
    spec = {s.name: s for s in DualMomentum.parameter_spec()}
    assert spec["lookback_months"].tunable
    assert spec["lookback_months"].default == 12
    assert DualMomentum({"lookback_months": 6}).required_history_bars < (
        DualMomentum({}).required_history_bars
    )


def test_holds_the_us_equity_when_it_leads_and_beats_cash():
    lake = _lake(_closes({"SPY.US": 0.001, "EFA.US": 0.0005, "BIL.US": 0.0001}))
    assert _held(DualMomentum({}), lake, MONTH_END) == {"SPY.US"}
    assert DualMomentum({}).estimate_return("SPY.US", MONTH_END, lake) == 1.0


def test_holds_the_ex_us_equity_when_it_leads():
    lake = _lake(_closes({"SPY.US": 0.0003, "EFA.US": 0.0008, "BIL.US": 0.0001}))
    assert _held(DualMomentum({}), lake, MONTH_END) == {"EFA.US"}


def test_holds_bonds_when_no_equity_beats_cash():
    lake = _lake(
        _closes({"SPY.US": -0.0005, "EFA.US": 0.00005, "BIL.US": 0.0001, "AGG.US": 0.0002})
    )
    assert _held(DualMomentum({}), lake, MONTH_END) == {"AGG.US"}


def test_holds_the_month_end_pick_until_the_next_month_end():
    """A crash right after the February month end flips the 12-month
    returns, but the March decisions keep February's pick (monthly
    rebalance). The March month end then moves to bonds."""
    crash = (date(2024, 3, 1), {"SPY.US": -0.03, "EFA.US": -0.03, "BIL.US": 0.0001})
    lake = _lake(_closes({"SPY.US": 0.001, "EFA.US": 0.0005, "BIL.US": 0.0001}, after=crash))
    strategy = DualMomentum({})
    assert _held(strategy, lake, MONTH_END) == {"SPY.US"}
    assert _held(strategy, lake, MID_MONTH) == {"SPY.US"}
    assert _held(strategy, lake, datetime(2024, 3, 28)) == {"AGG.US"}


def test_never_reads_a_bar_after_the_decision():
    """Two lakes equal up to the decision and wildly different after it
    give the same answer (P12)."""
    calm = _lake(_closes({"SPY.US": 0.001, "EFA.US": 0.0005, "BIL.US": 0.0001}))
    flip = (date(2024, 3, 1), {"SPY.US": -0.05, "EFA.US": 0.05, "BIL.US": 0.0001})
    later = _lake(_closes({"SPY.US": 0.001, "EFA.US": 0.0005, "BIL.US": 0.0001}, after=flip))
    for as_of in (MONTH_END, datetime(2024, 2, 29, 23, 0), datetime(2024, 3, 1, 9, 30)):
        assert _held(DualMomentum({}), calm, as_of) == _held(DualMomentum({}), later, as_of)


def test_the_lookback_changes_the_pick():
    """SPY led for a year, EFA led over the last three months."""
    turn = (date(2023, 12, 1), {"SPY.US": 0.0002, "EFA.US": 0.002, "BIL.US": 0.0001})
    lake = _lake(_closes({"SPY.US": 0.002, "EFA.US": 0.0003, "BIL.US": 0.0001}, after=turn))
    assert _held(DualMomentum({}), lake, MONTH_END) == {"SPY.US"}
    assert _held(DualMomentum({"lookback_months": 3}), lake, MONTH_END) == {"EFA.US"}


def test_missing_data_holds_nothing():
    lake = _lake(_closes({"SPY.US": 0.001, "EFA.US": 0.0005}), skip=("BIL.US",))
    assert _held(DualMomentum({}), lake, MONTH_END) == set()
    short = _lake(_closes({"SPY.US": 0.001}))
    assert _held(DualMomentum({}), short, datetime(2022, 6, 30)) == set()
    assert DualMomentum({}).estimate_return("SPY.US", MONTH_END, None) is None


def test_custom_tickers():
    closes = _closes({"SPY.US": 0.001, "EFA.US": 0.0005, "BIL.US": 0.0001})
    closes["VTI.US"] = closes.pop("SPY.US")
    lake = _lake(closes)
    strategy = DualMomentum({"us_equity": "VTI.US"})
    assert strategy.estimate_return("VTI.US", MONTH_END, lake) == 1.0
    assert "VTI.US" in strategy_data_tickers(strategy)


def test_tickers_must_differ():
    with pytest.raises(ValueError, match="different"):
        DualMomentum({"ex_us_equity": "SPY.US"})


def test_decide_switches_on_month_ends_only():
    strategy = DualMomentum({})
    portfolio = Portfolio(cash=10_000.0, positions={"SPY.US": 50.0})
    prices = dict.fromkeys(TICKERS, 100.0)
    picks = [(1.0, "AGG.US")]
    assert strategy.decide(picks, portfolio, prices, date(2024, 3, 14)) == []
    orders = strategy.decide(picks, portfolio, prices, date(2024, 2, 29))
    sides = {o.ticker: o.side for o in orders}
    assert sides == {"SPY.US": "sell", "AGG.US": "buy"}
    assert all(o.strategy_id == "dual_momentum" for o in orders)

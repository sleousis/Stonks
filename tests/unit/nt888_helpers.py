"""Shared fixtures for the neurotrader888 filter / ML strategy tests."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from stonks.core.interval import Interval
from stonks.core.types import Order
from stonks.store.lake import DuckDBLake
from stonks.strategies.base import BaseStrategy

STUB = "tests.unit.nt888_helpers:FittedStub"
SCRIPTED = "tests.unit.nt888_helpers:ScriptedSignal"


def write_bars(
    lake: DuckDBLake,
    ticker: str,
    dates: pd.DatetimeIndex,
    closes: np.ndarray,
    volumes: np.ndarray | None = None,
    spread: float = 0.01,
) -> None:
    closes = np.asarray(closes, dtype=float)
    if volumes is None:
        volumes = np.full(len(closes), 1_000_000.0)
    opens = np.concatenate([[closes[0]], closes[:-1]])
    lake.upsert_bars(
        pd.DataFrame(
            {
                "ticker": ticker,
                "timestamp": [d.to_pydatetime() for d in dates],
                "open": opens,
                "high": np.maximum(opens, closes) * (1 + spread),
                "low": np.minimum(opens, closes) * (1 - spread),
                "close": closes,
                "adj_close": closes,
                "volume": np.asarray(volumes, dtype=float),
            }
        ),
        Interval.DAY_1,
    )


def make_lake(path: Path) -> DuckDBLake:
    lake = DuckDBLake(path)
    lake.migrate()
    return lake


def _order(strategy_id: str, side: str, ticker: str, qty: float, as_of) -> Order:
    return Order(
        client_id=f"{strategy_id}:{side}:{ticker}:{as_of}",
        ticker=ticker,
        side=side,
        quantity=qty,
        order_type="market",
        strategy_id=strategy_id,
    )


class _LongOnlySingle(BaseStrategy):
    """Buys all cash when picked, sells everything when not."""

    applicable_asset_classes = ("crypto", "equity")

    def decide(self, my_picks, portfolio, prices, as_of):
        target = self.params["ticker"]
        holding = portfolio.positions.get(target, 0.0)
        price = prices.get(target)
        if my_picks and holding <= 0 and price and portfolio.cash > 0:
            return [_order(self.id, "buy", target, portfolio.cash / price, as_of)]
        if not my_picks and holding > 0:
            return [_order(self.id, "sell", target, holding, as_of)]
        return []


class FittedStub(_LongOnlySingle):
    """Always long its ticker; ``fit`` records a value that must survive
    save / load."""

    id = "fitted_stub"

    @classmethod
    def parameter_spec(cls):
        from stonks.core.params import ParameterSpec

        return [
            ParameterSpec(
                name="ticker", kind="categorical", default="X.US", bounds=None, tunable=False
            )
        ]

    def __init__(self, params):
        super().__init__(params)
        self.fitted_value: float | None = None

    def fit(self, dataset):
        self.fitted_value = 42.0

    def estimate_return(self, ticker, as_of, lake):
        return 1.0 if ticker == self.params["ticker"] else None

    def save(self, path):
        super().save(path)
        if self.fitted_value is not None:
            (Path(path) / "fitted_state.json").write_text(json.dumps({"v": self.fitted_value}))

    @classmethod
    def load(cls, path):
        inst = super().load(path)
        f = Path(path) / "fitted_state.json"
        if f.exists():
            inst.fitted_value = json.loads(f.read_text())["v"]
        return inst


class ScriptedSignal(_LongOnlySingle):
    """Long whenever the close is above ``level`` (a deterministic signal
    the tests can script through the bars themselves)."""

    id = "scripted"

    @classmethod
    def parameter_spec(cls):
        from stonks.core.params import ParameterSpec

        return [
            ParameterSpec(
                name="ticker", kind="categorical", default="X.US", bounds=None, tunable=False
            ),
            ParameterSpec(name="level", kind="float", default=100.0, bounds=(0.0, 1e9)),
        ]

    def __init__(self, params):
        super().__init__(params)
        self.calls = 0

    def estimate_return(self, ticker, as_of, lake):
        self.calls += 1
        if ticker != self.params["ticker"] or lake is None:
            return None
        bars = lake.get_bars(ticker, Interval.DAY_1, start=pd.Timestamp("1900-01-01"), end=as_of)
        if bars.empty:
            return None
        close = float(bars["close"].iloc[-1])
        return 0.01 if close > float(self.params["level"]) else None

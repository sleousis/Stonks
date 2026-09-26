"""Momentum — a technical-indicator reference strategy.

Computes the N-day trailing return and estimates expected forward return to
be that same value (a crude but standard proxy for momentum). Decide logic:
buy the top-ranked ticker with available cash; sell any existing holdings
that no longer appear in the current ranking.
"""

from __future__ import annotations

import weakref
from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

import numpy as np

from stonks.core.params import ParameterSpec
from stonks.core.types import Features, Order, Portfolio
from stonks.strategies.base import BaseStrategy

# Bounds wide enough to cover every daily bar a lake can hold.
_HISTORY_START = date(1900, 1, 1)
_HISTORY_END = date(2200, 1, 1)


class Momentum(BaseStrategy):
    id = "momentum"

    def __init__(self, params: Any) -> None:
        super().__init__(params)
        # Per-lake, per-ticker daily closes, loaded once per instance. A
        # backtest calls estimate_return for every ticker on every bar, and
        # re-querying the same history each time dominated tuning runtime.
        # Keyed weakly by lake so MCPT/perturbation lakes don't share entries.
        # Instances are short-lived (one per trial or per tick), so rows
        # added to the lake after the first read are not expected.
        self._closes: weakref.WeakKeyDictionary[Any, dict[str, tuple[np.ndarray, np.ndarray]]]
        self._closes = weakref.WeakKeyDictionary()

    @classmethod
    def parameter_spec(cls):
        return [
            ParameterSpec(
                name="lookback_days",
                kind="int",
                default=20,
                bounds=(5, 252),
                description="Lookback window for trailing return.",
            ),
            ParameterSpec(
                name="threshold",
                kind="float",
                default=0.0,
                bounds=(-1.0, 1.0),
                description="Minimum trailing return to become a pick.",
            ),
            ParameterSpec(
                name="allocation",
                kind="float",
                default=1.0,
                bounds=(0.0, 1.0),
                tunable=False,
                description="Fraction of cash deployed on the top pick.",
            ),
        ]

    def extract_features(self, ticker: str, as_of: date, lake: Any) -> Features:
        r = self._lookback_return(ticker, as_of, lake)
        return Features(values={"r_lookback": r} if r is not None else {})

    def estimate_return(self, ticker: str, as_of: date, lake: Any) -> float | None:
        r = self._lookback_return(ticker, as_of, lake)
        if r is None:
            return None
        return r if r > self.params["threshold"] else None

    def decide(
        self,
        my_picks: Sequence[tuple[float, str]],
        portfolio: Portfolio,
        prices: Mapping[str, float],
        as_of: date,
    ) -> list[Order]:
        orders: list[Order] = []

        picked_tickers = {t for _, t in my_picks}

        # 1. sell positions that fell out of the ranking
        for ticker, qty in list(portfolio.positions.items()):
            if qty > 0 and ticker not in picked_tickers:
                orders.append(
                    Order(
                        client_id=f"{self.id}:sell:{ticker}:{as_of.isoformat()}",
                        ticker=ticker,
                        side="sell",
                        quantity=qty,
                        order_type="market",
                        strategy_id=self.id,
                    )
                )

        # 2. buy the top pick if we don't already hold it
        if my_picks:
            sorted_picks = sorted(my_picks, key=lambda p: p[0], reverse=True)
            _, top = sorted_picks[0]
            price = prices.get(top)
            if (
                price
                and price > 0
                and portfolio.positions.get(top, 0.0) <= 0
                and portfolio.cash > 0
            ):
                qty = (portfolio.cash * float(self.params["allocation"])) / price
                if qty > 0:
                    orders.append(
                        Order(
                            client_id=f"{self.id}:buy:{top}:{as_of.isoformat()}",
                            ticker=top,
                            side="buy",
                            quantity=qty,
                            order_type="market",
                            strategy_id=self.id,
                        )
                    )

        return orders

    # ---- internals ---------------------------------------------------------

    def _lookback_return(self, ticker: str, as_of: date, lake: Any) -> float | None:
        if lake is None:
            return None
        lookback = int(self.params["lookback_days"])
        dates, closes = self._daily_closes(ticker, lake)
        # Only bars on or before ``as_of`` are visible: no look-ahead.
        end = int(np.searchsorted(dates, np.datetime64(as_of, "D"), side="right"))
        if end < lookback + 1:
            return None
        past, now = closes[end - lookback - 1], closes[end - 1]
        if past <= 0:
            return None
        return float(now / past - 1.0)

    def _daily_closes(self, ticker: str, lake: Any) -> tuple[np.ndarray, np.ndarray]:
        try:
            per_lake = self._closes.setdefault(lake, {})
        except TypeError:  # lake can't be weakly referenced; skip the cache
            per_lake = {}
        if ticker not in per_lake:
            df = lake.get_prices(ticker, start=_HISTORY_START, end=_HISTORY_END)
            if df is None or df.empty:
                per_lake[ticker] = (np.array([], dtype="datetime64[D]"), np.array([]))
            else:
                per_lake[ticker] = (
                    np.asarray(df["date"], dtype="datetime64[D]"),
                    df["close"].to_numpy(dtype=float),
                )
        return per_lake[ticker]

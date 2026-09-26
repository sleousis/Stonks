"""Shared helpers for the W3.1 risk-rule tests: a ``RiskPolicy`` carrying the
rule settings and synthetic adjusted OHLCV history."""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import date

import pandas as pd

from stonks.config import RiskPolicy
from stonks.core.types import Order, Portfolio
from stonks.production.rules import RiskContext
from stonks.production.rules.settings import RuleSettings

AS_OF = date(2025, 6, 30)


#: ``RiskPolicy`` carries ``rules`` itself now; the alias keeps the tests' name.
Policy = RiskPolicy


def policy(**rules) -> Policy:
    return Policy(rules=RuleSettings.model_validate(rules))


def buy(ticker: str, qty: float, i: int = 0, tick_id: str | None = None) -> Order:
    return Order(
        client_id=f"b:{ticker}:{i}", ticker=ticker, side="buy", quantity=qty, tick_id=tick_id
    )


def sell(ticker: str, qty: float, i: int = 0, tick_id: str | None = None) -> Order:
    return Order(
        client_id=f"s:{ticker}:{i}", ticker=ticker, side="sell", quantity=qty, tick_id=tick_id
    )


def bars(
    closes: Sequence[float],
    *,
    spread: float = 0.0,
    volume: float | Sequence[float] = 1_000_000.0,
    end: date = AS_OF,
) -> pd.DataFrame:
    """Daily bars ending at ``end`` (business days, oldest first); high/low
    are ``close +- spread`` and open is the previous close."""
    idx = pd.bdate_range(end=pd.Timestamp(end), periods=len(closes))
    close = pd.Series(list(closes), index=idx, dtype=float)
    vol = [volume] * len(closes) if isinstance(volume, int | float) else list(volume)
    return pd.DataFrame(
        {
            "open": close.shift(1).fillna(close.iloc[0]),
            "high": close + spread,
            "low": close - spread,
            "close": close,
            "volume": pd.Series(vol, index=idx, dtype=float),
        }
    ).rename_axis("date")


def alternating(n: int, start: float = 100.0, step: float = 0.01) -> list[float]:
    """Closes whose log returns alternate ``+step, -step``."""
    return [start * math.exp(step * (i % 2)) for i in range(n)]


def context(
    portfolio: Portfolio,
    prices: dict[str, float],
    pol: RiskPolicy,
    **kw,
) -> RiskContext:
    kw.setdefault("as_of", AS_OF)
    kw.setdefault("asset_classes", dict.fromkeys({*prices, *portfolio.positions}, "equity"))
    return RiskContext(portfolio=portfolio, prices=prices, policy=pol, **kw)

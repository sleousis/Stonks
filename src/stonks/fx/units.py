"""Price units: pence and pounds (review leftovers 19.18).

Some markets quote in a minor unit. EODHD sends London prices in pence,
and the lake keeps them as sent, marked by the instrument's currency
(``GBX``). Brokers and cash work in the major unit (pounds). Production
reads prices through :func:`price_scales` so every order's decision price,
limit, stop and sizing is in the major unit. Research keeps the lake's
units: a backtest prices, fills and values in one unit, so returns are the
same.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from stonks.fx.rates import normalize_currency


def quote_scale(currency: object) -> float:
    """What a price quoted in ``currency`` is multiplied by to be in the
    major unit: 0.01 for ``GBX`` (pence), 1.0 for a major currency or none."""
    if not isinstance(currency, str) or not currency.strip():
        return 1.0
    return normalize_currency(currency)[1]


def price_scales(lake: Any, tickers: Iterable[str]) -> dict[str, float]:
    """``{ticker: scale}`` for the tickers the lake quotes in a minor unit
    (``instruments.currency``). Tickers in a major unit are left out."""
    wanted = sorted(set(tickers))
    if not wanted or lake is None:
        return {}
    df = lake.sql(
        "SELECT id, currency FROM instruments WHERE id = ANY(?) AND currency IS NOT NULL",
        [wanted],
    )
    out: dict[str, float] = {}
    for record in df.to_dict("records"):
        scale = quote_scale(record["currency"])
        if scale != 1.0:
            out[str(record["id"])] = scale
    return out

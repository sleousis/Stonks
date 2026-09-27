"""Vertical spreads: two options of one right and one expiry.

- ``bull_call_spread`` (debit): buy the ``long_delta`` call, sell the
  ``short_delta`` call above it.
- ``bear_put_spread`` (debit): buy the ``long_delta`` put, sell the
  ``short_delta`` put below it.
- ``bull_put_spread`` (credit): sell the ``short_delta`` put, buy the
  ``long_delta`` put below it.
- ``bear_call_spread`` (credit): sell the ``short_delta`` call, buy the
  ``long_delta`` call above it.

``dte`` sets the expiry. The combo carries a net limit at the mid plus
``limit_slack`` (default 1.0) of the summed half spreads, so a spread
never pays more than both touches. Sizing: ``intent.quantity`` or one.
"""

from __future__ import annotations

from stonks.options.orders import ComboLeg, ComboOrder
from stonks.options.structures import BuildRequest, register_structure
from stonks.options.structures._common import chain_of, combo, param, pick


def _vertical(
    req: BuildRequest, right: str, buy_delta: float, sell_delta: float
) -> ComboOrder | None:
    chain = chain_of(req)
    if chain is None:
        return None
    expiry = req.selector.expiry(chain, param(req, "dte", 45))
    if expiry is None:
        return None
    buy = pick(req, right, buy_delta, 0, expiry=expiry)
    sell = pick(req, right, sell_delta, 0, expiry=expiry)
    if buy is None or sell is None or buy.contract.strike == sell.contract.strike:
        return None
    return combo(
        req,
        [ComboLeg("buy", 1, buy.contract), ComboLeg("sell", 1, sell.contract)],
        req.intent.quantity or 1,
        limit_from=[(buy, 1), (sell, -1)],
        limit_slack=float(param(req, "limit_slack", 1.0)),
    )


@register_structure("bull_call_spread")
def bull_call_spread(req: BuildRequest) -> ComboOrder | None:
    return _vertical(req, "call", param(req, "long_delta", 0.50), param(req, "short_delta", 0.25))


@register_structure("bear_put_spread")
def bear_put_spread(req: BuildRequest) -> ComboOrder | None:
    return _vertical(req, "put", param(req, "long_delta", 0.50), param(req, "short_delta", 0.25))


@register_structure("bull_put_spread")
def bull_put_spread(req: BuildRequest) -> ComboOrder | None:
    return _vertical(req, "put", param(req, "long_delta", 0.15), param(req, "short_delta", 0.30))


@register_structure("bear_call_spread")
def bear_call_spread(req: BuildRequest) -> ComboOrder | None:
    return _vertical(req, "call", param(req, "long_delta", 0.15), param(req, "short_delta", 0.30))

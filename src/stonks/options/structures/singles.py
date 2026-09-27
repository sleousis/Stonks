"""Single-leg structures: long calls and puts, covered calls, cash-secured
puts and protective puts.

Parameters (``intent.params``): ``delta`` (absolute target delta), ``dte``
(target days to expiry). Sizing when ``intent.quantity`` is unset:

- covered call: one contract per ``multiplier`` shares held and not yet
  covered;
- cash-secured put: as many contracts as ``allocation`` (default 1.0) of
  the equity can secure at the strike, less the cash already promised to
  short puts, and never more than the free cash;
- protective put: one contract per ``multiplier`` shares held;
- long call or put: one contract.
"""

from __future__ import annotations

import math

from stonks.options.orders import ComboLeg, ComboOrder
from stonks.options.structures import BuildRequest, register_structure
from stonks.options.structures._common import (
    combo,
    covered_calls,
    param,
    pick,
    reserved_put_cash,
)


def _long(req: BuildRequest, right: str, delta: float) -> ComboOrder | None:
    quote = pick(req, right, param(req, "delta", delta), param(req, "dte", 45))
    if quote is None:
        return None
    qty = req.intent.quantity or 1
    return combo(req, [ComboLeg("buy", 1, quote.contract)], qty)


@register_structure("long_call")
def long_call(req: BuildRequest) -> ComboOrder | None:
    return _long(req, "call", 0.5)


@register_structure("long_put")
def long_put(req: BuildRequest) -> ComboOrder | None:
    return _long(req, "put", 0.5)


@register_structure("covered_call")
def covered_call(req: BuildRequest) -> ComboOrder | None:
    underlying = req.intent.underlying
    quote = pick(req, "call", param(req, "delta", 0.30), param(req, "dte", 35))
    if quote is None:
        return None
    free = req.ctx.shares(underlying) - covered_calls(req, underlying) * quote.contract.multiplier
    qty = req.intent.quantity or math.floor(free / quote.contract.multiplier + 1e-9)
    return combo(req, [ComboLeg("sell", 1, quote.contract)], qty)


@register_structure("cash_secured_put")
def cash_secured_put(req: BuildRequest) -> ComboOrder | None:
    quote = pick(req, "put", param(req, "delta", 0.30), param(req, "dte", 35))
    if quote is None:
        return None
    per_contract = quote.contract.strike * quote.contract.multiplier
    budget = float(param(req, "allocation", 1.0)) * req.ctx.equity - reserved_put_cash(req)
    budget = min(budget, req.ctx.ledger.cash - reserved_put_cash(req))
    qty = req.intent.quantity or math.floor(max(budget, 0.0) / per_contract + 1e-9)
    return combo(req, [ComboLeg("sell", 1, quote.contract)], qty)


@register_structure("protective_put")
def protective_put(req: BuildRequest) -> ComboOrder | None:
    quote = pick(req, "put", param(req, "delta", 0.25), param(req, "dte", 60))
    if quote is None:
        return None
    held = req.ctx.shares(req.intent.underlying)
    qty = req.intent.quantity or math.floor(held / quote.contract.multiplier + 1e-9)
    return combo(req, [ComboLeg("buy", 1, quote.contract)], qty)

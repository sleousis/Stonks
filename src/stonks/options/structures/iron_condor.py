"""Iron condor: a short strangle with long wings, all one expiry.

Sell the ``short_delta`` put and call (default 0.16), buy the
``wing_delta`` put and call beyond them (default 0.05). The loss is
capped at the wider wing less the credit. Net limit and sizing as for the
verticals.
"""

from __future__ import annotations

from stonks.options.orders import ComboLeg, ComboOrder
from stonks.options.structures import BuildRequest, register_structure
from stonks.options.structures._common import chain_of, combo, param, pick


@register_structure("iron_condor")
def iron_condor(req: BuildRequest) -> ComboOrder | None:
    chain = chain_of(req)
    if chain is None:
        return None
    expiry = req.selector.expiry(chain, param(req, "dte", 45))
    if expiry is None:
        return None
    short_d = param(req, "short_delta", 0.16)
    wing_d = param(req, "wing_delta", 0.05)
    sp = pick(req, "put", short_d, 0, expiry=expiry)
    lp = pick(req, "put", wing_d, 0, expiry=expiry)
    sc = pick(req, "call", short_d, 0, expiry=expiry)
    lc = pick(req, "call", wing_d, 0, expiry=expiry)
    if sp is None or lp is None or sc is None or lc is None:
        return None
    if not (lp.contract.strike < sp.contract.strike < sc.contract.strike < lc.contract.strike):
        return None
    return combo(
        req,
        [
            ComboLeg("buy", 1, lp.contract),
            ComboLeg("sell", 1, sp.contract),
            ComboLeg("sell", 1, sc.contract),
            ComboLeg("buy", 1, lc.contract),
        ],
        req.intent.quantity or 1,
        limit_from=[(lp, 1), (sp, -1), (sc, -1), (lc, 1)],
        limit_slack=float(param(req, "limit_slack", 1.0)),
    )

"""Close a position group: trade every option leg the other way.

``intent.group_id`` names the group. Shares a group holds (after an
assignment) are left alone unless ``include_shares`` is true. No net
limit: an exit is never held back by price.
"""

from __future__ import annotations

from stonks.options.orders import ComboLeg, ComboOrder
from stonks.options.structures import BuildRequest, register_structure


@register_structure("close_group")
def close_group(req: BuildRequest) -> ComboOrder | None:
    ledger = req.ctx.ledger
    group = ledger.groups.get(req.intent.group_id or "")
    if group is None:
        return None
    include_shares = bool(req.intent.params.get("include_shares", False))
    legs: list[ComboLeg] = []
    for instrument, qty in sorted(group.legs.items()):
        if abs(qty) < 1e-9:
            continue
        side = "sell" if qty > 0 else "buy"
        contract = ledger.contracts.get(instrument)
        if contract is not None:
            legs.append(ComboLeg(side, abs(qty), contract))
        elif include_shares:
            legs.append(ComboLeg(side, abs(qty), shares=instrument))
    if not legs:
        return None
    return ComboOrder(
        client_id=req.client_id,
        legs=tuple(legs),
        quantity=1.0,
        structure="close_group",
        group_id=group.group_id,
        effect="close",
        strategy_id=req.strategy_id,
        decided_at=req.ctx.as_of,
        reason=req.intent.reason,
    )

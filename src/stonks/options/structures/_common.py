"""Helpers shared by the structure builders."""

from __future__ import annotations

import math
from typing import Any

from stonks.options.chain import ChainSnapshot, OptionQuote
from stonks.options.orders import ComboLeg, ComboOrder
from stonks.options.structures import BuildRequest


def param(req: BuildRequest, name: str, default: Any) -> Any:
    return req.intent.params.get(name, default)


def chain_of(req: BuildRequest) -> ChainSnapshot | None:
    chain = req.ctx.chains.get(req.intent.underlying)
    return chain if chain is not None and len(chain) else None


def pick(req: BuildRequest, right: str, delta: float, dte: int, expiry=None) -> OptionQuote | None:
    chain = chain_of(req)
    if chain is None:
        return None
    return req.selector.by_delta(chain, right, delta, dte, expiry=expiry)


def combo(
    req: BuildRequest,
    legs: list[ComboLeg],
    quantity: float,
    *,
    limit_from: list[tuple[OptionQuote, int]] | None = None,
    limit_slack: float | None = None,
) -> ComboOrder | None:
    """A combo of ``legs``; ``None`` when ``quantity`` is under one unit.

    With ``limit_from`` (quote, sign) pairs and ``limit_slack`` the net
    limit is the mid net price plus that share of the summed half spreads."""
    quantity = math.floor(quantity + 1e-9)
    if quantity < 1:
        return None
    net_limit = None
    if limit_from is not None and limit_slack is not None:
        mid = sum(sign * (q.mid or 0.0) for q, sign in limit_from)
        half = sum((q.half_spread or 0.0) for q, _ in limit_from)
        net_limit = round(mid + limit_slack * half, 4)
    return ComboOrder(
        client_id=req.client_id,
        legs=tuple(legs),
        quantity=float(quantity),
        structure=req.intent.structure,
        net_limit=net_limit,
        group_id=req.client_id,
        effect="open",
        strategy_id=req.strategy_id,
        decided_at=req.ctx.as_of,
        reason=req.intent.reason,
    )


def reserved_put_cash(req: BuildRequest) -> float:
    """Cash already promised to short puts in the book."""
    ledger = req.ctx.ledger
    return sum(
        -q * ledger.contracts[cid].strike * ledger.contracts[cid].multiplier
        for cid, q in ledger.options.items()
        if q < 0 and ledger.contracts[cid].right == "put"
    )


def covered_calls(req: BuildRequest, underlying: str) -> float:
    """Short call contracts already written on ``underlying``."""
    ledger = req.ctx.ledger
    return sum(
        -q
        for cid, q in ledger.options.items()
        if q < 0
        and ledger.contracts[cid].right == "call"
        and ledger.contracts[cid].underlying == underlying
    )

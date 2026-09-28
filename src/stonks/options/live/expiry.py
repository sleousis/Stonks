"""Expiry handling for live option books (roadmap 17.8).

A short option must never expire in the money unattended. Two steps:

1. **Plan** (after the close, :func:`plan_expiry`): every option position
   with ``close_sessions`` sessions or fewer left gets a closing combo (buy
   back a short, sell a long when ``close_longs``). With ``action = roll``
   a short is rolled instead: one combo that buys it back and sells the
   same right at the strike nearest the old one, on the expiry nearest
   ``roll_target_days`` out. No roll target falls back to a close. The
   combos become tickets like any option order.
2. **Watch** (on expiry day, :func:`expiry_watch`): a short option that
   expires today and is still held, in the money or within
   ``watch_band`` of its strike, raises a high urgency alert for the owner.

Client ids are stable per portfolio, day, contract and action, so a
second run of the same day adds no ticket.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta

from stonks.core.combos import ComboLeg, ComboOrder
from stonks.core.options import OptionContract, is_option_id, parse_contract_id
from stonks.logging import get_logger
from stonks.options.chain import OptionQuote
from stonks.options.live.settings import ExpirySettings

_log = get_logger("stonks.options.live.expiry")

#: Sessions from the day after ``as_of`` through ``expiry``, both ends
#: counted on the exchange calendar.
SessionsLeft = Callable[[date, date], int]


def weekday_sessions(as_of: date, expiry: date) -> int:
    """Weekdays after ``as_of`` up to ``expiry`` (a calendar-free fallback)."""
    days = 0
    d = as_of + timedelta(days=1)
    while d <= expiry:
        days += d.weekday() < 5
        d += timedelta(days=1)
    return days


def calendar_sessions(code: str = "XNYS") -> SessionsLeft:
    """Sessions left on an exchange calendar, the weekday count when the
    calendar does not cover the dates."""

    def count(as_of: date, expiry: date) -> int:
        if expiry <= as_of:
            return 0
        try:
            from stonks.scheduling.calendar import get_calendar

            return len(get_calendar(code).sessions(as_of + timedelta(days=1), expiry))
        except Exception as exc:
            _log.warning("options.expiry.calendar_fallback", error=str(exc))
            return weekday_sessions(as_of, expiry)

    return count


def combo_id(portfolio_id: str, as_of: date, contract_id: str, action: str) -> str:
    digest = hashlib.sha256(
        f"{portfolio_id}|{as_of.isoformat()}|{contract_id}|{action}".encode()
    ).hexdigest()[:16]
    return f"cmb-{digest}"


@dataclass(frozen=True)
class ExpiryItem:
    contract: OptionContract
    quantity: float
    sessions_left: int
    combo: ComboOrder


def _roll_target(
    contract: OptionContract, chain: Sequence[OptionContract], as_of: date, target_days: int
) -> OptionContract | None:
    later = [
        c
        for c in chain
        if c.underlying == contract.underlying
        and c.right == contract.right
        and c.multiplier == contract.multiplier
        and c.expiry > contract.expiry
    ]
    if not later:
        return None
    expiry = min({c.expiry for c in later}, key=lambda e: (abs((e - as_of).days - target_days), e))
    same = [c for c in later if c.expiry == expiry]
    return min(same, key=lambda c: (abs(c.strike - contract.strike), c.strike))


def plan_expiry(
    positions: Mapping[str, float],
    as_of: date,
    settings: ExpirySettings,
    *,
    portfolio_id: str,
    sessions_left: SessionsLeft = weekday_sessions,
    chain: Callable[[str], Sequence[OptionContract]] | None = None,
) -> list[ExpiryItem]:
    """The closes and rolls due for ``positions`` on ``as_of``."""
    items: list[ExpiryItem] = []
    for instrument, qty in sorted(positions.items()):
        if abs(qty) < 1e-9 or not is_option_id(instrument):
            continue
        contract = parse_contract_id(instrument)
        if contract.expiry < as_of:
            continue
        left = sessions_left(as_of, contract.expiry)
        if left > settings.close_sessions:
            continue
        if qty > 0 and not settings.close_longs:
            continue
        close_leg = ComboLeg("buy" if qty < 0 else "sell", 1, contract=contract)
        target = None
        if qty < 0 and settings.action == "roll" and chain is not None:
            target = _roll_target(
                contract, chain(contract.underlying), as_of, settings.roll_target_days
            )
        if target is not None:
            combo = ComboOrder(
                client_id=combo_id(portfolio_id, as_of, instrument, "roll"),
                legs=(close_leg, ComboLeg("sell", 1, contract=target)),
                quantity=abs(qty),
                structure="expiry_roll",
                effect="open",
                decided_at=as_of,
                reason=f"roll {instrument} to {target.contract_id}: {left} session(s) left",
            )
        else:
            combo = ComboOrder(
                client_id=combo_id(portfolio_id, as_of, instrument, "close"),
                legs=(close_leg,),
                quantity=abs(qty),
                structure="expiry_close",
                effect="close",
                decided_at=as_of,
                reason=f"close {instrument}: {left} session(s) left before expiry",
            )
        items.append(ExpiryItem(contract, qty, left, combo))
    return items


@dataclass(frozen=True)
class ExpiryWarning:
    contract: OptionContract
    quantity: float
    spot: float | None
    in_the_money: bool

    @property
    def text(self) -> str:
        where = "in the money" if self.in_the_money else "near the money"
        spot = f" (spot {self.spot:g})" if self.spot is not None else " (no spot)"
        return (
            f"short {self.contract.contract_id} x{abs(self.quantity):g} expires today {where}{spot}"
        )


def expiry_watch(
    positions: Mapping[str, float],
    as_of: date,
    settings: ExpirySettings,
    *,
    spots: Mapping[str, float],
    quotes: Mapping[str, OptionQuote] | None = None,
) -> list[ExpiryWarning]:
    """Short options that expire on ``as_of`` and need a person now. With no
    spot the position is always reported: it cannot be shown safe."""
    out: list[ExpiryWarning] = []
    for instrument, qty in sorted(positions.items()):
        if qty >= 0 or not is_option_id(instrument):
            continue
        contract = parse_contract_id(instrument)
        if contract.expiry != as_of:
            continue
        spot = spots.get(contract.underlying)
        if spot is None and quotes is not None and instrument in quotes:
            spot = quotes[instrument].underlying_price
        if spot is None:
            out.append(ExpiryWarning(contract, qty, None, True))
            continue
        itm = contract.intrinsic(spot) > 0
        near = abs(spot - contract.strike) <= settings.watch_band * contract.strike
        if itm or near:
            out.append(ExpiryWarning(contract, qty, spot, itm))
    return out

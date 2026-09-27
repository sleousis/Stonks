"""The simulated options broker (roadmap 17.3).

Fills come from the day's quotes only. A buy fills at ``mid + f x
half_spread`` and a sell at ``mid - f x half_spread``, with ``f``
(``spread_fraction``) 1.0 by default, which is at the touch. A leg with no
two-sided quote does not fill, and a model price never fills. Shares fill
at the day's price plus or minus ``share_slippage_bps``.

A :class:`~stonks.options.orders.ComboOrder` fills all of its legs or none:
every leg needs a quote, the net price per unit must be within
``net_limit``, and the cash after the fill may not go below zero (a cash
account; margin is the option risk rules' job). Fees are per contract and
per share. Orders are idempotent by ``client_id``: placing one twice
returns the first result.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date

from pydantic import BaseModel, ConfigDict, Field

from stonks.backtest.options_ledger import LedgerEvent, OptionLedger, PositionGroup
from stonks.options.chain import OptionQuote
from stonks.options.orders import ComboOrder


class OptionFillSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    #: Share of the half spread paid on each leg (0 = mid, 1 = touch).
    spread_fraction: float = Field(default=1.0, ge=0.0, le=5.0)
    fee_per_contract: float = Field(default=0.65, ge=0.0)
    fee_per_share: float = Field(default=0.005, ge=0.0)
    share_slippage_bps: float = Field(default=5.0, ge=0.0)


@dataclass(frozen=True)
class LegFill:
    instrument: str
    quantity: float
    price: float
    fee: float


@dataclass(frozen=True)
class ComboFill:
    client_id: str
    as_of: date
    legs: tuple[LegFill, ...]
    #: Net price per unit per share of the option contracts (+ debit).
    net_price: float
    cash_delta: float


@dataclass(frozen=True)
class Rejection:
    client_id: str
    as_of: date
    reason: str


@dataclass
class OptionsSimulatedBroker:
    ledger: OptionLedger
    settings: OptionFillSettings = field(default_factory=OptionFillSettings)
    _done: dict[str, ComboFill | Rejection] = field(
        default_factory=dict[str, "ComboFill | Rejection"]
    )

    def leg_price(self, quote: OptionQuote, side: str, f: float | None = None) -> float | None:
        mid, half = quote.mid, quote.half_spread
        if mid is None or half is None:
            return None
        share = self.settings.spread_fraction if f is None else f
        return mid + share * half if side == "buy" else max(mid - share * half, 0.0)

    def share_price(self, price: float, side: str) -> float:
        slip = self.settings.share_slippage_bps / 10_000.0
        return price * (1 + slip) if side == "buy" else price * (1 - slip)

    def quote_combo(
        self,
        combo: ComboOrder,
        quotes: Mapping[str, OptionQuote],
        spots: Mapping[str, float],
    ) -> tuple[list[LegFill], float] | str:
        """The fills the combo would get now, or why it would not fill."""
        legs: list[LegFill] = []
        net = 0.0
        multiplier = None
        for leg in combo.legs:
            qty = leg.sign * leg.ratio * combo.quantity
            if leg.contract is not None:
                quote = quotes.get(leg.contract.contract_id)
                price = None if quote is None else self.leg_price(quote, leg.side)
                if price is None:
                    return f"no two-sided quote for {leg.contract.contract_id}"
                fee = self.settings.fee_per_contract * abs(qty)
                net += leg.sign * leg.ratio * price
                multiplier = multiplier or leg.contract.multiplier
            else:
                spot = spots.get(str(leg.shares))
                if spot is None:
                    return f"no price for {leg.shares}"
                price = self.share_price(spot, leg.side)
                fee = self.settings.fee_per_share * abs(qty)
                net += leg.sign * leg.ratio * price / (multiplier or 100.0)
            legs.append(LegFill(leg.instrument, qty, price, fee))
        return legs, net

    def place(
        self,
        combo: ComboOrder,
        quotes: Mapping[str, OptionQuote],
        spots: Mapping[str, float],
        as_of: date,
    ) -> ComboFill | Rejection:
        if combo.client_id in self._done:
            return self._done[combo.client_id]
        priced = self.quote_combo(combo, quotes, spots)
        if isinstance(priced, str):
            return self._reject(combo, as_of, priced)
        legs, net = priced
        if combo.net_limit is not None and net > combo.net_limit + 1e-9:
            return self._reject(
                combo, as_of, f"net {net:.4f} beyond the limit {combo.net_limit:.4f}"
            )
        cash_delta = 0.0
        contracts = {leg.contract.contract_id: leg.contract for leg in combo.option_legs}
        for fill in legs:
            contract = contracts.get(fill.instrument)
            if contract is not None:
                cash_delta += -fill.quantity * fill.price * contract.multiplier - fill.fee
            else:
                cash_delta += -fill.quantity * fill.price - fill.fee
        if self.ledger.cash + cash_delta < -1e-6:
            return self._reject(
                combo, as_of, f"needs {-cash_delta:.2f} cash, has {self.ledger.cash:.2f}"
            )
        gid = combo.group_id or combo.client_id
        if combo.effect == "open" and gid not in self.ledger.groups:
            self.ledger.open_group(
                PositionGroup(
                    group_id=gid,
                    structure=combo.structure,
                    legs={},
                    opened_at=as_of,
                    open_cost=-cash_delta,
                    strategy_id=combo.strategy_id,
                )
            )
        for fill in legs:
            contract = contracts.get(fill.instrument)
            if contract is not None:
                self.ledger.fill_option(
                    contract, fill.quantity, fill.price, fill.fee, as_of, group_id=gid
                )
            else:
                self.ledger.fill_shares(
                    fill.instrument, fill.quantity, fill.price, fill.fee, as_of, group_id=gid
                )
        result = ComboFill(combo.client_id, as_of, tuple(legs), net, cash_delta)
        self._done[combo.client_id] = result
        return result

    def _reject(self, combo: ComboOrder, as_of: date, reason: str) -> Rejection:
        rejection = Rejection(combo.client_id, as_of, reason)
        self._done[combo.client_id] = rejection
        self.ledger.record(
            LedgerEvent(as_of, "rejected", combo.client_id, combo.quantity, detail=reason)
        )
        return rejection

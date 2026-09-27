"""The options book of a backtest (roadmap 17.3).

:class:`OptionLedger` holds cash, share positions, option positions (in
contracts, keyed by contract id), the contracts themselves and the
position groups that tie legs of one structure together. Every change
goes through a method that records a :class:`LedgerEvent`, so the book can
be audited line by line.

Money rules
-----------
- An option fill of ``q`` contracts (signed) at ``p`` per share moves cash
  by ``-q x p x multiplier - fee``.
- Exercise and assignment at expiry (physical): a call moves ``q x
  multiplier`` shares in and ``q x multiplier x strike`` cash out; a put
  the reverse. ``q`` is signed, so a short call being assigned delivers
  shares and receives the strike.
- Cash settlement credits ``q x multiplier x intrinsic``.
- Marks: an option is marked at its quote's mark (mid, else last) times
  its multiplier; :meth:`OptionLedger.value` takes the marks it is given.

Portfolio view
--------------
:meth:`OptionLedger.portfolio` returns a plain
:class:`~stonks.core.types.Portfolio` with option prices in contract-value
units (per-share mark x multiplier), so ``Portfolio.total_value`` gives the
right equity without core knowing about multipliers. The option risk rules
read that view.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from stonks.core.options import OptionContract, adjust_for_split
from stonks.core.types import Portfolio

EventKind = Literal[
    "fill",
    "share_fill",
    "exercise",
    "assignment",
    "cash_settlement",
    "expired_worthless",
    "early_assignment",
    "assignment_risk",
    "split",
    "dividend",
    "rejected",
]

_EPS = 1e-9


@dataclass(frozen=True)
class LedgerEvent:
    as_of: date
    kind: EventKind
    instrument: str
    quantity: float = 0.0
    price: float = 0.0
    cash_delta: float = 0.0
    fee: float = 0.0
    group_id: str | None = None
    detail: str = ""


@dataclass
class PositionGroup:
    group_id: str
    structure: str
    #: Instrument -> signed quantity the group holds.
    legs: dict[str, float]
    opened_at: date
    #: Net cash paid to open (negative for a credit), fees included.
    open_cost: float = 0.0
    strategy_id: str | None = None


@dataclass
class OptionLedger:
    cash: float
    shares: dict[str, float] = field(default_factory=dict[str, float])
    options: dict[str, float] = field(default_factory=dict[str, float])
    contracts: dict[str, OptionContract] = field(default_factory=dict[str, OptionContract])
    groups: dict[str, PositionGroup] = field(default_factory=dict[str, PositionGroup])
    events: list[LedgerEvent] = field(default_factory=list[LedgerEvent])

    # ---- views ----------------------------------------------------------------

    def positions(self) -> dict[str, float]:
        """Every position: shares by ticker, options by contract id."""
        return {**self.shares, **self.options}

    def option_value(self, marks: Mapping[str, float]) -> float:
        return sum(
            q * marks.get(cid, 0.0) * self.contracts[cid].multiplier
            for cid, q in self.options.items()
        )

    def value(self, spots: Mapping[str, float], marks: Mapping[str, float]) -> float:
        """Cash plus shares at ``spots`` plus options at per-share ``marks``."""
        shares = sum(q * spots.get(t, 0.0) for t, q in self.shares.items())
        return self.cash + shares + self.option_value(marks)

    def portfolio(
        self, spots: Mapping[str, float], marks: Mapping[str, float]
    ) -> tuple[Portfolio, dict[str, float]]:
        """A plain portfolio and its prices, options in contract-value units."""
        prices = {t: spots[t] for t in self.shares if t in spots}
        for cid in self.options:
            if cid in marks:
                prices[cid] = marks[cid] * self.contracts[cid].multiplier
        for t, s in spots.items():
            prices.setdefault(t, s)
        return Portfolio(cash=self.cash, positions=self.positions()), prices

    # ---- changes --------------------------------------------------------------

    def _move(self, book: dict[str, float], key: str, qty: float) -> None:
        new = book.get(key, 0.0) + qty
        if abs(new) < _EPS:
            book.pop(key, None)
        else:
            book[key] = new

    def _group_leg(self, group_id: str | None, instrument: str, qty: float) -> None:
        if group_id is None or group_id not in self.groups:
            return
        legs = self.groups[group_id].legs
        self._move(legs, instrument, qty)
        if not legs:
            del self.groups[group_id]

    def open_group(self, group: PositionGroup) -> None:
        self.groups[group.group_id] = group

    def fill_option(
        self,
        contract: OptionContract,
        quantity: float,
        price: float,
        fee: float,
        as_of: date,
        group_id: str | None = None,
    ) -> float:
        """Apply an option fill; returns the cash delta."""
        cid = contract.contract_id
        self.contracts[cid] = contract
        cash_delta = -quantity * price * contract.multiplier - fee
        self.cash += cash_delta
        self._move(self.options, cid, quantity)
        self._group_leg(group_id, cid, quantity)
        self.events.append(
            LedgerEvent(as_of, "fill", cid, quantity, price, cash_delta, fee, group_id)
        )
        return cash_delta

    def fill_shares(
        self,
        ticker: str,
        quantity: float,
        price: float,
        fee: float,
        as_of: date,
        group_id: str | None = None,
        kind: EventKind = "share_fill",
        detail: str = "",
    ) -> float:
        cash_delta = -quantity * price - fee
        self.cash += cash_delta
        self._move(self.shares, ticker, quantity)
        self._group_leg(group_id, ticker, quantity)
        self._trim_share_groups(ticker)
        self.events.append(
            LedgerEvent(as_of, kind, ticker, quantity, price, cash_delta, fee, group_id, detail)
        )
        return cash_delta

    def _trim_share_groups(self, ticker: str) -> None:
        """Groups never claim more shares than the book holds: shares that
        left through an assignment or a sale leave the newest groups first."""
        held = self.shares.get(ticker, 0.0)
        claimed = [(gid, g.legs[ticker]) for gid, g in self.groups.items() if ticker in g.legs]
        excess = sum(q for _, q in claimed if q > 0) - max(held, 0.0)
        for gid, qty in reversed(claimed):
            if excess <= _EPS:
                break
            if qty <= 0:
                continue
            take = min(qty, excess)
            excess -= take
            self._group_leg(gid, ticker, -take)
        # a short share leg in a group only stands for shares actually short
        for gid, g in list(self.groups.items()):
            q = g.legs.get(ticker, 0.0)
            if q < 0 and held >= 0:
                self._group_leg(gid, ticker, -q)

    def group_of(self, instrument: str) -> str | None:
        for gid, group in self.groups.items():
            if instrument in group.legs:
                return gid
        return None

    def settle(
        self, contract_id: str, spot: float, as_of: date, *, early: bool = False
    ) -> list[LedgerEvent]:
        """Exercise, assign, cash-settle or expire the whole position in
        ``contract_id`` with the underlying at ``spot``."""
        qty = self.options.get(contract_id, 0.0)
        if abs(qty) < _EPS:
            return []
        contract = self.contracts[contract_id]
        gid = self.group_of(contract_id)
        before = len(self.events)
        self._move(self.options, contract_id, -qty)
        self._group_leg(gid, contract_id, -qty)
        exercise = early or contract.auto_exercises(spot)
        if not exercise:
            self.events.append(
                LedgerEvent(as_of, "expired_worthless", contract_id, qty, 0.0, 0.0, 0.0, gid)
            )
            return self.events[before:]
        units = qty * contract.multiplier
        if contract.settlement == "cash":
            cash = units * contract.intrinsic(spot)
            self.cash += cash
            self.events.append(
                LedgerEvent(as_of, "cash_settlement", contract_id, qty, spot, cash, 0.0, gid)
            )
            return self.events[before:]
        kind: EventKind = "early_assignment" if early else ("exercise" if qty > 0 else "assignment")
        share_qty = units if contract.is_call else -units
        # the shares change hands at the strike; they join the book, not the
        # option's group (a strategy decides what to do with them)
        self.fill_shares(
            contract.underlying,
            share_qty,
            contract.strike,
            0.0,
            as_of,
            kind=kind,
            detail=f"{contract_id} group {gid}" if gid else contract_id,
        )
        return self.events[before:]

    def apply_split(self, ticker: str, ratio: float, as_of: date) -> list[LedgerEvent]:
        """Shares times ``ratio``; every option on ``ticker`` becomes its
        adjusted contract (``core.options.adjust_for_split``), groups too."""
        before = len(self.events)
        if ticker in self.shares:
            old = self.shares[ticker]
            self.shares[ticker] = old * ratio
            self.events.append(
                LedgerEvent(
                    as_of, "split", ticker, old * ratio - old, 0.0, 0.0, 0.0, None, f"x{ratio}"
                )
            )
            for group in self.groups.values():
                if ticker in group.legs:
                    group.legs[ticker] *= ratio
        for cid in [c for c in self.options if self.contracts[c].underlying == ticker]:
            qty = self.options.pop(cid)
            new_contract, new_qty = adjust_for_split(self.contracts[cid], qty, ratio)
            new_id = new_contract.contract_id
            self.contracts[new_id] = new_contract
            self._move(self.options, new_id, new_qty)
            for group in self.groups.values():
                if cid in group.legs:
                    group.legs[new_id] = group.legs.pop(cid) * (new_qty / qty)
            self.events.append(
                LedgerEvent(as_of, "split", cid, new_qty, 0.0, 0.0, 0.0, None, f"-> {new_id}")
            )
        return self.events[before:]

    def apply_dividend(
        self, ticker: str, amount: float, as_of: date, withholding: float = 0.0
    ) -> LedgerEvent | None:
        """A cash dividend on held shares; a short pays it."""
        qty = self.shares.get(ticker, 0.0)
        if abs(qty) < _EPS or amount <= 0:
            return None
        rate = (1.0 - withholding) if qty > 0 else 1.0
        cash = qty * amount * rate
        self.cash += cash
        event = LedgerEvent(as_of, "dividend", ticker, qty, amount, cash)
        self.events.append(event)
        return event

    def record(self, event: LedgerEvent) -> None:
        self.events.append(event)

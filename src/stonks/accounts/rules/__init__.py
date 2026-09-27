"""The account rules engine (roadmap 19.7, ``docs/design/live-trading.md`` section 5).

What a real account may do under its jurisdiction and type. Each live
portfolio has an :class:`AccountProfile`: where the account is held
(``us``, ``eu`` or ``uk``, the broker entity, not the owner's passport),
``cash`` or ``margin``, and ``retail`` or ``professional``.

Rules are found automatically, like risk rules: every public module of
this package is imported and each class decorated with
:func:`register_account_rule` joins the registry. A rule declares which
jurisdictions and account types it applies to. For each order the engine
(:func:`run_account_rules`) returns a :class:`Verdict` per rule that
acted: ``clip`` (a smaller quantity), ``drop``, ``approve`` (a person must
approve it, roadmap 19.8) or ``note`` (tagged, unchanged).

A closing order is never dropped or shrunk (P28): a ``clip`` or ``drop``
on a close becomes ``approve``. Orders run sells first, then buys, each
through every rule against a running :class:`AccountBook`. Sale proceeds
do not add to settled cash (they settle later), so a cash account never
buys with unsettled money (no free-riding).

The broker is the source of truth for cash, settled cash, buying power and
day trades remaining. Our own counters (the settlement ledger, the day
trade count) explain a refusal early. When ours and the broker's disagree,
the stricter one wins.

From ``production`` this package imports only its settings model, which
sits next to the risk rule settings so the config can load it. The
``account_rules`` risk rule (``production/rules/account_rules.py``) adapts
the engine to the risk pipeline.
"""

from __future__ import annotations

import importlib
import math
import pkgutil
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping, MutableMapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any, ClassVar, Literal

from stonks.core.types import Order
from stonks.execution.brokers.base import AccountType, LiveAccountState
from stonks.production.rules._account_settings import AccountRulesSettings

__all__ = [
    "EPS",
    "AccountBook",
    "AccountProfile",
    "AccountRule",
    "AccountRuleInputs",
    "AccountRulesSettings",
    "ClientClass",
    "InstrumentFacts",
    "Jurisdiction",
    "OrderView",
    "SettlementEntry",
    "Verdict",
    "discover_account_rules",
    "market_of",
    "register_account_rule",
    "registered_account_rules",
    "run_account_rules",
]

EPS = 1e-9

Jurisdiction = Literal["us", "eu", "uk"]
ClientClass = Literal["retail", "professional"]
FxPolicy = Literal["refuse", "convert"]
WashSaleMode = Literal["warn", "block"]
VerdictAction = Literal["clip", "drop", "approve", "note"]


@dataclass(frozen=True)
class AccountProfile:
    portfolio_id: str
    jurisdiction: Jurisdiction
    #: The owner's first live account is a cash account, long only.
    account_type: AccountType = "cash"
    client_class: ClientClass = "retail"
    base_currency: str = "USD"
    fx_policy: FxPolicy = "refuse"
    wash_sale_mode: WashSaleMode = "warn"
    #: Shorts need a margin account (a later Phase 19 follow-up).
    allow_short: bool = False


@dataclass(frozen=True)
class InstrumentFacts:
    """What the rules need to know about a ticker, from the lake."""

    security_type: str | None = None
    isin: str | None = None
    currency: str | None = None
    shares_outstanding: float | None = None

    @property
    def domicile(self) -> str | None:
        """The ISIN's country code (``US``, ``IE``, ``GB``, ...)."""
        if self.isin and len(self.isin) >= 2 and self.isin[:2].isalpha():
            return self.isin[:2].upper()
        return None

    @property
    def is_fund(self) -> bool:
        return self.security_type in ("etf", "fund")


@dataclass(frozen=True)
class SettlementEntry:
    """One fill's cash and the day it settles (from the security's market)."""

    ticker: str
    side: Literal["buy", "sell"]
    currency: str
    #: Signed cash: negative for a buy, positive for a sell.
    amount: float
    trade_date: date
    settle_date: date

    def unsettled_on(self, day: date) -> bool:
        return self.settle_date > day


@dataclass(frozen=True)
class AccountRuleInputs:
    """Everything the account rules look at for one live book."""

    profile: AccountProfile
    #: The session the orders execute in.
    as_of: date
    account: LiveAccountState | None = None
    instruments: Mapping[str, InstrumentFacts] = field(default_factory=dict[str, InstrumentFacts])
    #: Tickers this portfolio must not buy, with the reason.
    restricted: Mapping[str, str] = field(default_factory=dict[str, str])
    #: Whether a fund has the key information document the profile's
    #: jurisdiction needs (EU and UK retail). Missing means unknown.
    kid_available: Mapping[str, bool] = field(default_factory=dict[str, bool])
    settlements: Sequence[SettlementEntry] = ()
    #: One date per day trade in the pattern day trader window.
    day_trades: Sequence[date] = ()
    #: Tickers bought in the execution session (a close of one is a day trade).
    opened_today: frozenset[str] = frozenset()
    #: The last loss sale per ticker (wash sale window).
    loss_sales: Mapping[str, date] = field(default_factory=dict[str, date])
    #: Shares available to borrow per ticker (``None``: shortable, amount
    #: unknown). A ticker missing here has no locate.
    shortable: Mapping[str, float | None] = field(default_factory=dict[str, float | None])
    #: Tickers under the short sale price test (US Rule 201).
    short_sale_restricted: frozenset[str] = frozenset()

    def unsettled_sales(self) -> float:
        """Sale proceeds not settled yet on ``as_of`` (base currency)."""
        return sum(
            e.amount for e in self.settlements if e.side == "sell" and e.unsettled_on(self.as_of)
        )


@dataclass(frozen=True)
class OrderView:
    """One order as the rules see it."""

    order: Order
    #: Opens or grows a position (a buy, or a short sale).
    opening: bool
    #: The price its cash is valued at: the limit when set, else the
    #: reference. ``None`` when unknown.
    price: float | None
    #: The currency it trades in.
    currency: str

    @property
    def ticker(self) -> str:
        return self.order.ticker

    @property
    def side(self) -> str:
        return self.order.side


@dataclass
class AccountBook:
    """The account as the allowed orders leave it (running, per run)."""

    #: Settled cash left to spend (cash accounts). ``None``: unknown.
    settled_cash: float | None
    #: Available funds left (margin accounts). ``None``: unknown.
    available_funds: float | None
    cash_by_currency: dict[str, float]
    positions: dict[str, float]
    day_trades_used: int = 0
    base_currency: str = "USD"

    def commit(self, view: OrderView, qty: float) -> None:
        signed = qty if view.side == "buy" else -qty
        held = self.positions.get(view.ticker, 0.0) + signed
        if abs(held) <= EPS:
            self.positions.pop(view.ticker, None)
        else:
            self.positions[view.ticker] = held
        if view.side != "buy" or view.price is None:
            return  # sale proceeds settle later: never spendable in this run
        cost = qty * view.price
        if self.settled_cash is not None:
            self.settled_cash -= cost
        if self.available_funds is not None:
            self.available_funds -= cost
        if view.currency in self.cash_by_currency:
            self.cash_by_currency[view.currency] -= cost


@dataclass(frozen=True)
class Verdict:
    rule: str
    action: VerdictAction
    #: The quantity the rule allows (0 for a drop).
    quantity: float
    reason: str
    ticker: str = ""
    side: str = ""
    original_quantity: float = 0.0


class AccountRule(ABC):
    """One account rule. Subclasses set ``name`` and ``order`` (lower runs
    first) and may narrow ``jurisdictions`` and ``account_types``."""

    name: ClassVar[str]
    order: ClassVar[int]
    #: ``None``: every jurisdiction.
    jurisdictions: ClassVar[frozenset[str] | None] = None
    #: ``None``: cash and margin accounts.
    account_types: ClassVar[frozenset[str] | None] = None

    def applies(self, profile: AccountProfile) -> bool:
        if self.jurisdictions is not None and profile.jurisdiction not in self.jurisdictions:
            return False
        return self.account_types is None or profile.account_type in self.account_types

    @abstractmethod
    def check(
        self,
        view: OrderView,
        qty: float,
        book: AccountBook,
        inputs: AccountRuleInputs,
        settings: AccountRulesSettings,
    ) -> Verdict | None:
        """A verdict when the rule acts on ``view`` (at ``qty``), else ``None``."""

    def verdict(self, view: OrderView, action: VerdictAction, qty: float, reason: str) -> Verdict:
        return Verdict(
            rule=self.name,
            action=action,
            quantity=max(qty, 0.0),
            reason=reason,
            ticker=view.ticker,
            side=view.side,
            original_quantity=view.order.quantity,
        )

    def clip_to(self, view: OrderView, qty: float, max_qty: float, why: str) -> Verdict | None:
        """``clip`` (or ``drop`` when nothing is left) when ``qty`` exceeds
        ``max_qty``, else ``None``."""
        if qty <= max_qty + EPS:
            return None
        allowed = max(max_qty, 0.0)
        if allowed <= EPS:
            return self.verdict(view, "drop", 0.0, why)
        return self.verdict(view, "clip", allowed, why)


def market_of(ticker: str) -> str:
    """The market code of an EODHD-style ticker (``AAPL.US`` -> ``US``)."""
    return ticker.rsplit(".", 1)[-1].upper() if "." in ticker else "US"


# ---- the engine ---------------------------------------------------------------------


def open_book(inputs: AccountRuleInputs, positions: Mapping[str, float]) -> AccountBook:
    """The running book at the start of a run. Settled cash is the
    stricter of the broker's figure and ours (cash minus unsettled sale
    proceeds)."""
    account = inputs.account
    settled: float | None = None
    available: float | None = None
    by_ccy: dict[str, float] = {}
    if account is not None:
        ours = account.cash - inputs.unsettled_sales()
        settled = min(account.settled_cash, ours)
        available = account.available_funds
        by_ccy = {k: float(v) for k, v in account.cash_by_currency.items()}
    return AccountBook(
        settled_cash=settled,
        available_funds=available,
        cash_by_currency=by_ccy,
        positions=dict(positions),
        day_trades_used=len(inputs.day_trades),
        base_currency=inputs.profile.base_currency,
    )


def is_opening(order: Order, positions: Mapping[str, float]) -> bool:
    if order.position_effect is not None:
        return order.position_effect == "open"
    held = positions.get(order.ticker, 0.0)
    return held >= -EPS if order.side == "buy" else held <= EPS


def run_account_rules(
    orders: Sequence[Order],
    inputs: AccountRuleInputs,
    settings: AccountRulesSettings,
    prices: Mapping[str, float],
    positions: Mapping[str, float],
    *,
    rules: Sequence[AccountRule] | None = None,
) -> tuple[list[Order], list[Verdict]]:
    """``orders`` as the account allows them (sells first, then buys, each
    kept in its original relative order) and every verdict."""
    from dataclasses import replace

    active = [
        r
        for r in (rules if rules is not None else registered_account_rules())
        if r.applies(inputs.profile)
    ]
    book = open_book(inputs, positions)
    allowed: dict[int, Order] = {}
    verdicts: list[Verdict] = []
    for side in ("sell", "buy"):
        for index, order in enumerate(orders):
            if order.side != side:
                continue
            view = OrderView(
                order=order,
                opening=is_opening(order, book.positions),
                price=_price(order, prices),
                currency=_currency(order.ticker, inputs),
            )
            qty: float | None = order.quantity
            for rule in active:
                v = rule.check(view, qty, book, inputs, settings)
                if v is None:
                    continue
                if not view.opening and v.action in ("clip", "drop"):
                    v = Verdict(
                        rule=v.rule,
                        action="approve",
                        quantity=qty,
                        reason=f"{v.reason}; a closing order is never dropped, it needs approval",
                        ticker=v.ticker,
                        side=v.side,
                        original_quantity=v.original_quantity,
                    )
                verdicts.append(v)
                if v.action == "drop":
                    qty = None
                    break
                if v.action == "clip":
                    qty = v.quantity
            if qty is None or qty <= EPS:
                continue
            book.commit(view, qty)
            allowed[index] = order if qty == order.quantity else replace(order, quantity=qty)
    return [allowed[i] for i in sorted(allowed)], verdicts


def _price(order: Order, prices: Mapping[str, float]) -> float | None:
    if order.limit_price is not None:
        return order.limit_price
    value = prices.get(order.ticker)
    try:
        v = float(value) if value is not None else math.nan
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) and v > 0 else None


def _currency(ticker: str, inputs: AccountRuleInputs) -> str:
    facts = inputs.instruments.get(ticker)
    if facts is not None and facts.currency:
        return facts.currency.upper()
    return inputs.profile.base_currency


# ---- registry -----------------------------------------------------------------------

_RULES: dict[str, type[AccountRule]] = {}


def register_account_rule(
    cls: type[AccountRule] | None = None,
    *,
    registry: MutableMapping[str, type[AccountRule]] | None = None,
) -> Any:
    """Class decorator adding a rule to the registry (names are unique)."""
    target = _RULES if registry is None else registry

    def add(rule_cls: type[AccountRule]) -> type[AccountRule]:
        existing = target.get(rule_cls.name)
        same = existing is not None and (existing.__module__, existing.__qualname__) == (
            rule_cls.__module__,
            rule_cls.__qualname__,
        )
        if existing is not None and not same:
            raise ValueError(
                f"account rule name {rule_cls.name!r} is used by both "
                f"{existing.__module__}.{existing.__qualname__} and "
                f"{rule_cls.__module__}.{rule_cls.__qualname__}"
            )
        target[rule_cls.name] = rule_cls
        return rule_cls

    return add if cls is None else add(cls)


def discover_account_rules() -> None:
    """Import every public module of this package so its rules register."""
    import stonks.accounts.rules as package

    for info in pkgutil.iter_modules(package.__path__):
        if not info.name.startswith("_"):
            importlib.import_module(f"{package.__name__}.{info.name}")


def registered_account_rules(
    applies_to: AccountProfile | None = None,
) -> list[AccountRule]:
    """One instance of every rule, by ``order`` then name (only those that
    apply to ``applies_to`` when given)."""
    discover_account_rules()
    rules = [cls() for cls in sorted(_RULES.values(), key=lambda c: (c.order, c.name))]
    return rules if applies_to is None else [r for r in rules if r.applies(applies_to)]


#: ``(profile) -> bool`` filters, for callers that list rules per profile.
RuleFilter = Callable[[AccountProfile], bool]

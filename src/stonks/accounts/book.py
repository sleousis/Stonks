"""``BookSpec``: the per-portfolio input of the construction pipeline, and
the tighten-only merges that build it.

The W2.1 pipeline takes one ``BookSpec`` per portfolio instead of reading
config, so it doesn't care where a book came from. Until the tick loops over
portfolios (step S6), :meth:`BookSpec.default` builds today's single book from
config; :meth:`BookSpec.for_portfolio` builds one from a portfolio row and its
subscriptions.

Risk overrides (portfolio ``risk_policy_json``, subscription
``risk_overrides_json``) are *partial* policies: only the keys they set take
part, and :func:`tighter_of` is the only function that merges them. It can
tighten a limit, never loosen one (principle P28). The design calls it
``RiskPolicy.tighter_of``; ``RiskPolicy`` lives in ``stonks.config``, so the
function lives here and the integration step may expose it as a static
method there.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal

from stonks.accounts.models import DEFAULT_PORTFOLIO_ID, Mode, Portfolio, Subscription
from stonks.config import RiskPolicy
from stonks.production.rules.settings import tighter_rule_settings

if TYPE_CHECKING:
    from stonks.config import Settings

BookBroker = Literal["simulated", "connection"]
PolicyOverride = RiskPolicy | Mapping[str, Any] | None


def _min_optional(a: int | None, b: int | None) -> int | None:
    """``None`` means unlimited, so any number is tighter."""
    if a is None:
        return b
    if b is None:
        return a
    return min(a, b)


def _min_caps(a: Mapping[str, float], b: Mapping[str, float]) -> dict[str, float]:
    """A class capped in either input is capped at the lower cap; adding a cap
    is tighter (it also blocks buys of tickers of unknown class)."""
    out = dict(a)
    for cls, cap in b.items():
        out[cls] = min(out[cls], cap) if cls in out else cap
    return out


#: How each ``RiskPolicy`` field tightens. Every field needs a rule (a unit
#: test checks it), and merging a field without one fails loudly.
MERGE_RULES: dict[str, Callable[[Any, Any], Any]] = {
    "enabled": lambda a, b: a or b,
    "max_open_positions": _min_optional,
    "max_weight_per_ticker": min,
    "max_weight_per_asset_class": _min_caps,
    "cash_buffer_fraction": max,
    "min_order_notional": max,
    "rules": tighter_rule_settings,
}


def _as_partial(override: PolicyOverride) -> tuple[RiskPolicy, frozenset[str]] | None:
    if override is None:
        return None
    policy = override if isinstance(override, RiskPolicy) else RiskPolicy.model_validate(override)
    return policy, frozenset(policy.model_fields_set)


def partial_risk_policy(overrides: Mapping[str, Any] | None) -> dict[str, Any]:
    """A partial ``RiskPolicy`` for storage: validated (bad values and
    unknown keys raise), keeping only the keys given."""
    policy = RiskPolicy.model_validate(dict(overrides or {}))
    return policy.model_dump(mode="json", include=set(policy.model_fields_set))


def tighter_of(base: RiskPolicy, *overrides: PolicyOverride) -> RiskPolicy:
    """``base`` tightened by each override. An override is a ``RiskPolicy``
    or a mapping of its fields (validated; unknown keys raise); only the
    fields it explicitly sets take part, so ``{}`` changes nothing. The
    result is never looser than ``base`` or any override on any field, and
    doesn't depend on the order of the overrides."""
    values = {name: getattr(base, name) for name in RiskPolicy.model_fields}
    for override in overrides:
        partial = _as_partial(override)
        if partial is None:
            continue
        policy, explicit = partial
        for name in explicit:
            rule = MERGE_RULES.get(name)
            if rule is None:
                raise ValueError(f"no tighten rule for RiskPolicy.{name}")
            values[name] = rule(values[name], getattr(policy, name))
    merged = RiskPolicy.model_validate(values)
    return base if merged == base else merged


#: Construction knobs shared by every constructor that bound risk (see
#: ``stonks.portfolio.base.ConstructorSettings``). Everything else in a
#: construction mapping (which constructor, its parameters) is the
#: portfolio's own choice and simply overrides.
CONSTRUCTION_TIGHTEN: dict[str, Callable[[Any, Any], Any]] = {
    "long_only": lambda a, b: bool(a) or bool(b),
    "max_gross": min,
}


def merge_construction(
    base: Mapping[str, Any], *overrides: Mapping[str, Any] | None
) -> dict[str, Any]:
    """Global construction settings merged with portfolio overrides. The W2.1
    pipeline validates the result against its constructor's ``Settings``.

    The shared risk knobs (:data:`CONSTRUCTION_TIGHTEN`) only tighten, also
    when the global settings leave one unset: then the constructor's default
    is the bound (``max_gross`` 1.0, ``long_only``), so a portfolio can never
    loosen what nobody configured (P26, P28). A knob nested in an
    override's ``params`` is held to the same rule."""
    out = dict(base)
    defaults = _construction_defaults()
    for override in overrides:
        flat = dict(override or {})
        nested = flat.pop("params", None)
        if isinstance(nested, Mapping):
            params = dict(nested)
            for key in CONSTRUCTION_TIGHTEN:
                if key in params:
                    flat.setdefault(key, params.pop(key))
            flat["params"] = params
        elif nested is not None:
            flat["params"] = nested
        for key, value in flat.items():
            rule = CONSTRUCTION_TIGHTEN.get(key)
            if rule is None:
                out[key] = value
            else:
                out[key] = rule(out[key] if key in out else defaults[key], value)
    return out


def _construction_defaults() -> dict[str, Any]:
    """The constructors' own defaults for the shared risk knobs."""
    from stonks.portfolio.base import ConstructorSettings

    fields = ConstructorSettings.model_fields
    return {key: fields[key].default for key in CONSTRUCTION_TIGHTEN}


def _frozen(mapping: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType(dict(mapping))


@dataclass(frozen=True)
class BookSpec:
    """Everything the construction pipeline needs to know about one book.

    - ``strategy_weights``: share of the risk budget per subscribed strategy;
      ``None`` means equal over whatever strategies send signals (today's
      single book).
    - ``strategy_modes``: paper or auto per strategy (only order-placing
      subscriptions are in the book).
    - ``risk``: ``tighter_of(global, portfolio)``; ``risk_overrides`` per
      strategy slice: ``tighter_of(global, portfolio, subscription)``.
    - ``construction``: global construction settings merged with the
      portfolio's (:func:`merge_construction`).
    - ``initial_cash``: seeds the book on its first tick.
    - ``universe``: ``None`` = the global universe.
    """

    portfolio_id: str
    risk: RiskPolicy
    broker: BookBroker
    strategy_weights: Mapping[str, float] | None = None
    strategy_modes: Mapping[str, Mode] = field(default_factory=lambda: _frozen({}))
    construction: Mapping[str, Any] = field(default_factory=lambda: _frozen({}))
    risk_overrides: Mapping[str, RiskPolicy] = field(default_factory=lambda: _frozen({}))
    allow_short: bool = False
    initial_cash: float = 0.0
    universe: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        # Read-only views, so a frozen spec can't be changed through a dict.
        for name in ("strategy_modes", "construction", "risk_overrides"):
            object.__setattr__(self, name, _frozen(getattr(self, name)))
        if self.strategy_weights is not None:
            object.__setattr__(self, "strategy_weights", _frozen(self.strategy_weights))

    @classmethod
    def default(cls, settings: Settings) -> BookSpec:
        """Today's single book, from config only: the default portfolio, the
        global risk policy, equal strategy weights, long-only."""
        prod = settings.production
        return cls(
            portfolio_id=DEFAULT_PORTFOLIO_ID,
            risk=prod.risk,
            broker="connection" if settings.brokers.kind != "simulated" else "simulated",
            initial_cash=float(prod.initial_cash),
            universe=tuple(prod.universe) if prod.universe else None,
        )

    @classmethod
    def for_portfolio(
        cls,
        portfolio: Portfolio,
        subscriptions: list[Subscription],
        *,
        global_risk: RiskPolicy,
        global_construction: Mapping[str, Any] | None = None,
        default_initial_cash: float,
        owner_risk: PolicyOverride = None,
    ) -> BookSpec:
        """The book of one portfolio. Only enabled paper and auto
        subscriptions of this portfolio take part; an auto-paused one places
        nothing, so it is left out. ``owner_risk``: the owner's own risk
        limits (``users.risk_policy_json``), which tighten every portfolio
        they own."""
        live = [
            s
            for s in subscriptions
            if s.portfolio_id == portfolio.id
            and s.enabled
            and s.mode.places_orders
            and not s.auto_paused
        ]
        risk = tighter_of(global_risk, owner_risk, portfolio.risk_policy)
        return cls(
            portfolio_id=portfolio.id,
            risk=risk,
            broker="connection" if portfolio.kind == "broker" else "simulated",
            strategy_weights={s.strategy_id: s.weight for s in live},
            strategy_modes={s.strategy_id: s.mode for s in live},
            construction=merge_construction(global_construction or {}, portfolio.construction),
            risk_overrides={s.strategy_id: tighter_of(risk, s.risk_overrides) for s in live},
            allow_short=portfolio.allow_short,
            initial_cash=(
                float(portfolio.initial_cash)
                if portfolio.initial_cash is not None
                else float(default_initial_cash)
            ),
            universe=portfolio.universe,
        )

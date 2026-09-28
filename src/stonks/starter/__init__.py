"""The starter set (complexity audit F19): a few simple, well-understood
strategies and a small liquid trading universe, so a fresh install is never
empty and invited traders have something to watch from day one.

Each starter is registered **On trial** (``shadow``) through the normal
registry, so it runs its own test book every trading run and must pass the
go-live check (or an admin's audited override) before anyone can follow it
with money. Nothing here approves a strategy. Installing is idempotent:
starters already registered (in any status, retired included) are skipped.

The trading universe is only set when none is configured: it is written as
a console override of ``production.universe`` (``stonks.config_overrides``),
so the admin sees it, changes it or resets it in Settings.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: Liquid US-listed ETFs across stocks, bonds, gold and real estate: enough
#: breadth for a momentum and a trend strategy, few enough to load fast.
STARTER_UNIVERSE: tuple[str, ...] = (
    "SPY.US",
    "QQQ.US",
    "IWM.US",
    "EFA.US",
    "EEM.US",
    "TLT.US",
    "IEF.US",
    "GLD.US",
    "VNQ.US",
    "XLE.US",
)

#: Actor written to the registry's history for an install the shell runs.
STARTER_REASON = "Starter strategy: a simple, well-known reference installed with Stonks"


@dataclass(frozen=True)
class StarterSpec:
    id: str
    class_path: str
    title: str
    summary: str
    params: dict[str, Any] = field(default_factory=dict)


STARTERS: tuple[StarterSpec, ...] = (
    StarterSpec(
        id="starter_buy_and_hold",
        class_path="stonks.strategies.examples.buy_and_hold:BuyAndHold",
        title="Starter: buy and hold the S&P 500",
        summary="Buys SPY once and holds it. The yardstick every other strategy must beat.",
        params={"ticker": "SPY.US", "allocation": 1.0},
    ),
    StarterSpec(
        id="starter_trend",
        class_path="stonks.strategies.examples.ewmac_trend:EWMACTrend",
        title="Starter: trend following",
        summary="Holds funds whose moving averages point up and steps aside when they turn "
        "down (Carver's EWMAC).",
    ),
    StarterSpec(
        id="starter_momentum",
        class_path="stonks.strategies.examples.momentum:Momentum",
        title="Starter: momentum",
        summary="Holds the fund that rose most over the last six months, skipping the last month.",
    ),
)

_BY_ID = {s.id: s for s in STARTERS}


def starter_info(strategy_id: str) -> StarterSpec | None:
    """The starter behind ``strategy_id``, or ``None`` for any other strategy."""
    return _BY_ID.get(strategy_id)


@dataclass(frozen=True)
class StarterInstall:
    registered: tuple[str, ...]
    skipped: tuple[str, ...]
    #: The universe written as the trading universe, or ``None`` when one
    #: was already configured (left alone).
    universe: tuple[str, ...] | None


def install_starters(
    state: Any,
    registry: Any,
    *,
    actor: str,
    current_universe: Any,
) -> StarterInstall:
    """Register every missing starter On trial and, when
    ``current_universe`` is empty, store :data:`STARTER_UNIVERSE` as the
    trading universe override. Never changes a strategy's status."""
    from stonks.config_overrides import OverrideStore
    from stonks.lab.catalog import load_strategy_class

    registered: list[str] = []
    skipped: list[str] = []
    for spec in STARTERS:
        if state.sql("SELECT 1 FROM strategies WHERE id = ?", [spec.id]):
            skipped.append(spec.id)
            continue
        strategy = load_strategy_class(spec.class_path)(dict(spec.params))  # type: ignore[call-arg]
        registry.register(strategy, reports=[], strategy_id=spec.id)
        registry.record_intervention(
            "config", actor=actor, reason=STARTER_REASON, strategy_id=spec.id
        )
        registered.append(spec.id)
    universe: tuple[str, ...] | None = None
    if not current_universe:
        universe = STARTER_UNIVERSE
        OverrideStore(state).set(
            "production.universe",
            list(universe),
            actor=actor,
            reason="Starter set: a small liquid trading universe",
        )
    return StarterInstall(tuple(registered), tuple(skipped), universe)

"""The options strategy contract (roadmap 17.5, design section 6).

An :class:`OptionStrategy` looks at one day (:class:`OptionDecisionContext`:
underlying history up to the decision, the day's chain snapshots and the
book) and returns what it wants: :class:`OptionIntent` values (``"sell a
0.30 delta call 35 days out on AAPL"``) or ready :class:`ComboOrder`
values. Intents go through the structure registry
(``stonks.options.structures``), which picks the contracts from the chain
with the :class:`~stonks.options.selector.LegSelector`. Selection logic
lives in one place and any equity view can drive an options overlay.

Strategies are discovered from ``stonks.options.strategies`` (one module
each) and every one states its hypothesis (P1). None is enabled anywhere
by default: they run in the options backtest and the lab validation only.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any, ClassVar

from stonks.backtest.options_ledger import OptionLedger, PositionGroup
from stonks.core.params import Params, ParamSpace, validate_params
from stonks.options.chain import ChainSnapshot
from stonks.options.orders import ComboOrder


@dataclass(frozen=True)
class OptionIntent:
    underlying: str
    structure: str
    params: Mapping[str, Any] = field(default_factory=dict[str, Any])
    #: Combo units to trade (``None``: the structure sizes it).
    quantity: float | None = None
    #: The group an exit intent closes.
    group_id: str | None = None
    reason: str = ""


@dataclass(frozen=True)
class OptionDecisionContext:
    as_of: date
    #: Underlying closes up to and including ``as_of``, oldest first.
    history: Mapping[str, Sequence[tuple[date, float]]]
    chains: Mapping[str, ChainSnapshot]
    ledger: OptionLedger
    equity: float
    #: The next known ex-dividend date and amount per underlying.
    next_dividends: Mapping[str, tuple[date, float]] = field(
        default_factory=dict[str, tuple[date, float]]
    )
    rate: float = 0.0

    def spot(self, underlying: str) -> float | None:
        series = self.history.get(underlying) or ()
        return series[-1][1] if series else None

    def closes(self, underlying: str) -> list[float]:
        return [c for _, c in self.history.get(underlying) or ()]

    def realized_vol(self, underlying: str, window: int = 21) -> float | None:
        """Annualised close-to-close vol over the last ``window`` returns."""
        closes = self.closes(underlying)[-(window + 1) :]
        if len(closes) < window + 1:
            return None
        rets = [math.log(b / a) for a, b in zip(closes, closes[1:], strict=False) if a > 0]
        if len(rets) < 2:
            return None
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        return math.sqrt(var * 252)

    def shares(self, underlying: str) -> float:
        return self.ledger.shares.get(underlying, 0.0)

    def groups(
        self, structure: str | None = None, underlying: str | None = None
    ) -> list[PositionGroup]:
        out = []
        for group in self.ledger.groups.values():
            if structure is not None and group.structure != structure:
                continue
            if underlying is not None and not any(
                k == underlying or k.startswith(f"{underlying}:") for k in group.legs
            ):
                continue
            out.append(group)
        return out


Decision = OptionIntent | ComboOrder


class OptionStrategy(ABC):
    id: ClassVar[str]
    hypothesis: ClassVar[str] = ""
    #: Structures the strategy opens (the short option guard checks them).
    structures: ClassVar[tuple[str, ...]] = ()

    def __init__(self, params: Params | None = None) -> None:
        space = self.parameter_spec()
        given = dict(params or {})
        validate_params(given, space)
        self.params: dict[str, Any] = {s.name: s.default for s in space} | given

    @classmethod
    @abstractmethod
    def parameter_spec(cls) -> ParamSpace: ...

    def underlyings(self, universe: Sequence[str]) -> list[str]:
        return list(universe)

    @abstractmethod
    def decide(self, ctx: OptionDecisionContext) -> list[Decision]: ...

"""The trade cost seam for strategies (roadmap 22.10).

A strategy whose decisions depend on what trading costs (``forecast_blend``
drops rules that cost too much) never reads config. The code that runs it
hands it a cost model instead:

- the lab binds the dataset's costs to every strategy it builds;
- a backtest binds the request's cost model, else ``[backtest.costs]``;
- the tick binds its ``[backtest.costs]`` to every strategy it loads.

A strategy opts in by implementing :class:`CostAware`. :func:`bind_costs`
reaches wrapped strategies too. Without a cost model, or with one that
charges nothing, :func:`trade_costs_or_default` gives
``CostModelSettings.realistic()``.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from stonks.backtest.costs import CostModelSettings

__all__ = ["CostAware", "bind_costs", "has_costs", "trade_costs_or_default"]


@runtime_checkable
class CostAware(Protocol):
    """A strategy that decides with trade costs."""

    def bind_costs(self, costs: CostModelSettings) -> None: ...


def has_costs(costs: CostModelSettings | None) -> bool:
    """``costs`` charges something: a fee or a spread on some asset class."""
    if costs is None:
        return False
    return any(
        c.fee_flat > 0 or c.fee_bps > 0 or c.half_spread_bps > 0
        for c in (costs.default, *costs.asset_classes.values())
    )


def trade_costs_or_default(costs: CostModelSettings | None) -> CostModelSettings:
    """``costs``, or the realistic defaults when it is missing or free."""
    return costs if costs is not None and has_costs(costs) else CostModelSettings.realistic()


def bind_costs[S](strategy: S, costs: CostModelSettings | None) -> S:
    """Hand ``costs`` to ``strategy`` and every strategy it wraps that is
    :class:`CostAware`. ``None`` changes nothing. Returns ``strategy``."""
    if costs is None:
        return strategy
    seen: set[int] = set()
    node: Any = strategy
    while node is not None and id(node) not in seen:
        seen.add(id(node))
        if isinstance(node, CostAware):
            node.bind_costs(costs)
        node = getattr(node, "inner", None)
    return strategy

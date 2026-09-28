"""Transaction costs for the production simulated broker, resolved in one place.

Precedence (``SimulatedCosts.from_settings``):

1. ``[backtest.costs]`` configured (the table is present, even with all
   zeros): the tick fills through that ``CostModelSettings`` model, the same
   one backtests use, and the legacy ``[production]`` ``slippage_bps`` /
   ``fee_per_trade`` are ignored, so no cost is ever charged twice.
2. Otherwise: the legacy flat ``[production]`` slippage and per-trade fee
   (``FixedCostModel``), for configs written before the cost model existed.

``make_broker`` (simulated) and the production tick both build their broker
through ``SimulatedCosts.build_broker``; nothing else constructs one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from stonks.backtest.costs import CostModelSettings
from stonks.backtest.fills import FillModel
from stonks.backtest.simulated_broker import SimulatedBroker
from stonks.core.types import Portfolio

if TYPE_CHECKING:  # pragma: no cover
    from stonks.config import Settings


@dataclass(frozen=True)
class SimulatedCosts:
    #: The per-asset-class cost model; ``None`` means the legacy flat costs.
    model: CostModelSettings | None = None
    slippage_bps: float = 0.0
    fee_per_trade: float = 0.0

    def __post_init__(self) -> None:
        if self.model is not None and (self.slippage_bps or self.fee_per_trade):
            raise ValueError(
                "use a cost model or legacy slippage_bps/fee_per_trade, not both "
                "(costs would be charged twice)"
            )

    @classmethod
    def from_settings(cls, settings: Settings) -> SimulatedCosts:
        if backtest_costs_configured(settings):
            return cls(model=settings.backtest.costs)
        p = settings.production
        return cls(slippage_bps=p.slippage_bps, fee_per_trade=p.fee_per_trade)

    def build_broker(
        self, portfolio: Portfolio, fill_model: FillModel | None = None
    ) -> SimulatedBroker:
        """An in-memory broker trading ``portfolio``. The caller still sets
        prices (and volumes / asset classes, which the model reads).
        ``fill_model`` (default: the whole order at the price given) is the
        one a paper book fills through at the next open."""
        if self.model is not None:
            return SimulatedBroker(
                portfolio=portfolio, cost_model=self.model.build(), fill_model=fill_model
            )
        return SimulatedBroker(
            portfolio=portfolio,
            slippage_bps=self.slippage_bps,
            fee_per_trade=self.fee_per_trade,
            fill_model=fill_model,
        )


def backtest_costs_configured(settings: Settings) -> bool:
    """True when ``[backtest.costs]`` was given (TOML, constructor or
    assignment), whatever its values."""
    return "costs" in settings.backtest.model_fields_set

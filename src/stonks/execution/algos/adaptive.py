"""IBKR's Adaptive algo: works a limit or collared market order between the
bid and the ask, more patient or more urgent by its priority. Only IBKR runs
it: at another broker the order goes out plain."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from stonks.execution.algos.base import (
    AlgoCostAssumption,
    AlgoWindow,
    ExecutionAlgo,
    NativeAlgo,
    register_algo,
)

Priority = Literal["patient", "normal", "urgent"]

_IB_PRIORITY: dict[str, str] = {"patient": "Patient", "normal": "Normal", "urgent": "Urgent"}
#: A patient order waits for a better price inside the spread: it pays less
#: of the half spread, and drifts a little more while it waits.
_COSTS: dict[str, AlgoCostAssumption] = {
    "patient": AlgoCostAssumption(spread_factor=0.5, impact_factor=1.0, timing_bps=1.0),
    "normal": AlgoCostAssumption(spread_factor=0.75, impact_factor=1.0, timing_bps=0.5),
    "urgent": AlgoCostAssumption(spread_factor=0.9, impact_factor=1.0, timing_bps=0.0),
}


class AdaptiveParams(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    priority: Priority = "normal"


@register_algo
class Adaptive(ExecutionAlgo):
    name = "adaptive"
    title = "Adaptive"
    description = (
        "IBKR works the order between the bid and the ask. Patient waits longer for a better "
        "price, urgent fills sooner. Other brokers send a plain order."
    )
    params_model = AdaptiveParams
    sliceable = False

    def cost_assumption(self, params: Mapping[str, Any]) -> AlgoCostAssumption:
        return _COSTS[str(params.get("priority", "normal"))]

    def native(self, params: Mapping[str, Any], window: AlgoWindow | None) -> NativeAlgo:
        priority = _IB_PRIORITY[str(params.get("priority", "normal"))]
        return NativeAlgo(strategy="Adaptive", params=(("adaptivePriority", priority),))

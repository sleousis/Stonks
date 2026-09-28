"""TWAP: the same number of shares in each part of a window. IBKR runs it
natively. At another broker Stonks sends ``slices`` equal child orders,
evenly spaced from the window start."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from pydantic import ConfigDict, Field, model_validator

from stonks.execution.algos.base import (
    AlgoCostAssumption,
    AlgoWindow,
    ChildSlice,
    ExecutionAlgo,
    NativeAlgo,
    WindowParams,
    ib_time,
    register_algo,
    session_window,
    slices_over,
)

#: Spreading an order over hours cuts its impact, but it pays the spread on
#: every slice and drifts with the market meanwhile.
COST = AlgoCostAssumption(spread_factor=1.0, impact_factor=0.6, timing_bps=2.0)


class TwapParams(WindowParams):
    model_config = ConfigDict(extra="forbid", frozen=True)

    start_minutes: int = Field(default=0, ge=0, le=1440, description="Minutes after the open.")
    end_minutes: int | None = Field(
        default=None, ge=1, le=1440, description="Minutes after the open; empty runs to the close."
    )
    slices: int = Field(default=6, ge=1, le=100, description="Child orders Stonks sends.")

    @model_validator(mode="after")
    def _ordered(self) -> TwapParams:
        if self.end_minutes is not None and self.end_minutes <= self.start_minutes:
            raise ValueError("end_minutes must be after start_minutes")
        return self


@register_algo
class Twap(ExecutionAlgo):
    name = "twap"
    title = "TWAP"
    description = (
        "Equal parts over a window of the session. IBKR runs it natively, other brokers get "
        "equal child orders from Stonks."
    )
    params_model = TwapParams
    sliceable = True

    def cost_assumption(self, params: Mapping[str, Any]) -> AlgoCostAssumption:
        return COST

    def window(
        self, params: Mapping[str, Any], session_open: datetime, session_close: datetime
    ) -> AlgoWindow:
        return session_window(params, session_open, session_close)

    def native(self, params: Mapping[str, Any], window: AlgoWindow | None) -> NativeAlgo:
        tags: list[tuple[str, str]] = [("strategyType", "Marketable")]
        if window is not None:
            tags += [("startTime", ib_time(window.start)), ("endTime", ib_time(window.end))]
        tags.append(("allowPastEndTime", "0"))
        return NativeAlgo(strategy="Twap", params=tuple(tags))

    def slices(
        self, quantity: int, params: Mapping[str, Any], window: AlgoWindow
    ) -> list[ChildSlice]:
        count = max(1, min(int(params.get("slices", 6)), quantity))
        return slices_over(quantity, window, [1.0] * count)

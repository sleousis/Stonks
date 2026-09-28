"""VWAP: trade with the day's volume, more near the open and the close. IBKR
runs it natively with a participation cap. At another broker Stonks sends
child orders sized by a typical U-shaped intraday volume curve."""

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

#: Trading with the volume cuts impact the most, and drifts with the day.
COST = AlgoCostAssumption(spread_factor=1.0, impact_factor=0.5, timing_bps=2.0)


def volume_curve(points: int) -> list[float]:
    """A typical U-shaped intraday volume share at ``points`` evenly spaced
    moments of a window: ``1 + 2 (2t - 1)^2`` at each slot's middle."""
    out: list[float] = []
    for i in range(points):
        t = (i + 0.5) / points
        out.append(1.0 + 2.0 * (2.0 * t - 1.0) ** 2)
    return out


class VwapParams(WindowParams):
    model_config = ConfigDict(extra="forbid", frozen=True)

    start_minutes: int = Field(default=0, ge=0, le=1440, description="Minutes after the open.")
    end_minutes: int | None = Field(
        default=None, ge=1, le=1440, description="Minutes after the open; empty runs to the close."
    )
    max_participation: float = Field(
        default=0.1, gt=0.0, le=0.5, description="Largest share of the market volume (IBKR)."
    )
    slices: int = Field(default=13, ge=1, le=100, description="Child orders Stonks sends.")

    @model_validator(mode="after")
    def _ordered(self) -> VwapParams:
        if self.end_minutes is not None and self.end_minutes <= self.start_minutes:
            raise ValueError("end_minutes must be after start_minutes")
        return self


@register_algo
class Vwap(ExecutionAlgo):
    name = "vwap"
    title = "VWAP"
    description = (
        "Trades with the day's volume inside a window. IBKR runs it natively with a "
        "participation cap, other brokers get child orders from Stonks on a volume curve."
    )
    params_model = VwapParams
    sliceable = True

    def cost_assumption(self, params: Mapping[str, Any]) -> AlgoCostAssumption:
        return COST

    def window(
        self, params: Mapping[str, Any], session_open: datetime, session_close: datetime
    ) -> AlgoWindow:
        return session_window(params, session_open, session_close)

    def native(self, params: Mapping[str, Any], window: AlgoWindow | None) -> NativeAlgo:
        tags: list[tuple[str, str]] = [("maxPctVol", f"{float(params['max_participation']):g}")]
        if window is not None:
            tags += [("startTime", ib_time(window.start)), ("endTime", ib_time(window.end))]
        tags += [("allowPastEndTime", "0"), ("noTakeLiq", "0")]
        return NativeAlgo(strategy="Vwap", params=tuple(tags))

    def slices(
        self, quantity: int, params: Mapping[str, Any], window: AlgoWindow
    ) -> list[ChildSlice]:
        count = max(1, min(int(params.get("slices", 13)), quantity))
        return slices_over(quantity, window, volume_curve(count))

"""``[production.risk_monitor]``: live risk monitoring settings.

A light module (pydantic only), so ``stonks.config`` can hold the model
without importing the monitoring math and its scipy dependency."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class RiskMonitorSettings(BaseModel):
    """Live risk monitoring, read by the ``risk_monitor`` tick hook."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = True
    #: EWMA decay (RiskMetrics daily 0.94).
    lam: float = Field(default=0.94, gt=0.0, lt=1.0)
    #: Daily returns behind a forecast, and days in the violation window.
    window: int = Field(default=250, ge=20, le=2000)
    #: Scored days before the violation ratio is judged (health, alerts).
    min_window: int = Field(default=60, ge=1)
    ratio_low: float = Field(default=0.5, ge=0.0)
    ratio_high: float = Field(default=1.5, gt=0.0)

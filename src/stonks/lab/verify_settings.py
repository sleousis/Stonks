"""``[lab.verify]`` settings (roadmap 23.9).

Example::

    [lab.verify]
    tolerance = 0.05          # a rerun score may differ by this much
    statuses = ["active"]     # what the weekly lab_verify job re-checks
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class LabVerifySettings(BaseModel):
    """``stonks lab verify`` and the weekly ``lab_verify`` job."""

    model_config = ConfigDict(extra="forbid")

    #: How far a rerun's objective score may drift from the stored one, in
    #: the objective's units (Sharpe for ``sharpe``), before it has moved.
    tolerance: float = Field(default=0.05, ge=0.0)
    #: Registry statuses the weekly job re-checks.
    statuses: list[Literal["active", "shadow"]] = Field(default_factory=lambda: ["active"])

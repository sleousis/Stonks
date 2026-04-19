"""Canonical ingestion row schemas.

These are the *normalized* rows that flow from any `DataSource` into the lake —
vendor-specific shapes are translated up-front, so the pipeline and the lake
never see vendor fields.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict


class RawPriceBar(BaseModel):
    model_config = ConfigDict(frozen=True)

    ticker: str
    date: date
    open: float
    high: float
    low: float
    close: float
    adj_close: float
    volume: int | None = None


class FundamentalRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    ticker: str
    period_end: date
    frequency: Literal["Q", "A"]
    statement: Literal["income", "balance", "cashflow"]
    line_item: str
    value: float | None

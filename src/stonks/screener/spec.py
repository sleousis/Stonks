"""What a screen asks for: the universe rule's filters (listed on the date,
asset class, sector, exchange, price, dollar volume), an optional stored
universe to start from, bounds on registered metrics, and an order."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from stonks.lab.universe import UniverseRule

#: Most metric filters and extra columns one screen may name.
MAX_FILTERS = 20
#: Most rows one screen returns.
MAX_LIMIT = 5000


def _known(metric: str) -> str:
    from stonks.screener.registry import metric_ids

    if metric not in metric_ids():
        raise ValueError(f"unknown metric {metric!r}; choose one of {metric_ids()}")
    return metric


class MetricFilter(BaseModel):
    """Keep a ticker when ``min <= value <= max``. A ticker with no value
    for the metric fails the filter."""

    model_config = ConfigDict(extra="forbid")

    metric: str = Field(max_length=64)
    min: float | None = None
    max: float | None = None

    @field_validator("metric")
    @classmethod
    def _metric(cls, v: str) -> str:
        return _known(v)

    @model_validator(mode="after")
    def _bounds(self) -> Self:
        if self.min is None and self.max is None:
            raise ValueError(f"{self.metric}: give min or max")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError(f"{self.metric}: min must be at most max")
        return self


class ScreenSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # the universe rule's filters (stonks.lab.universe.UniverseRule)
    asset_classes: list[Literal["equity", "crypto", "commodity", "bond"]] | None = None
    sectors: list[str] | None = None
    exclude_sectors: list[str] = Field(default_factory=list)
    exchanges: list[str] | None = None
    min_price: float | None = Field(default=None, ge=0)
    min_adv: float | None = Field(default=None, ge=0)
    adv_window_bars: int = Field(default=20, ge=1, le=2520)
    #: Start from the members of this stored universe on the date.
    universe_id: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9_.-]{0,63}$")
    filters: list[MetricFilter] = Field(default_factory=list, max_length=MAX_FILTERS)
    #: Order by this metric (tickers without a value last); default: by ticker.
    sort_by: str | None = Field(default=None, max_length=64)
    descending: bool = True
    #: Keep the first ``limit`` rows after sorting (a top N).
    limit: int | None = Field(default=None, ge=1, le=MAX_LIMIT)
    #: More metrics to show next to the ones filtered and sorted on.
    columns: list[str] = Field(default_factory=list, max_length=MAX_FILTERS)

    @field_validator("sort_by")
    @classmethod
    def _sort(cls, v: str | None) -> str | None:
        return None if v is None else _known(v)

    @field_validator("columns")
    @classmethod
    def _columns(cls, v: list[str]) -> list[str]:
        return [_known(c) for c in dict.fromkeys(v)]

    def rule(self) -> UniverseRule:
        return UniverseRule(
            min_adv=self.min_adv,
            asset_classes=tuple(self.asset_classes) if self.asset_classes is not None else None,
            exclude_sectors=tuple(self.exclude_sectors),
            adv_window_bars=self.adv_window_bars,
            min_price=self.min_price,
            sectors=tuple(self.sectors) if self.sectors is not None else None,
            exchanges=tuple(self.exchanges) if self.exchanges is not None else None,
        )

    def shown_metrics(self) -> list[str]:
        """The metrics a result shows: filtered, sorted on, then columns."""
        names = [f.metric for f in self.filters]
        if self.sort_by is not None:
            names.append(self.sort_by)
        return list(dict.fromkeys([*names, *self.columns]))

"""``rule`` universes: point-in-time filters evaluated on the lake.

At each rebalance date between ``start`` and ``end`` (default: the refresh
date) the filters of :class:`~stonks.lab.universe.UniverseRule` pick the
members from what the lake knew on that date: listed and not yet
delisted, average daily dollar volume, last price, asset class, sector
and exchange. A name is a member from the rebalance date it qualified on
until the next rebalance date it failed. The rule reads only bars and
instrument rows already in the lake, so ingest (or ensure) the candidate
data first.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from stonks.lab.universe import UniverseRule, resolve
from stonks.universes.base import Materialized, MembershipSpan, RefreshContext, UniverseProvider

Rebalance = Literal["weekly", "monthly", "quarterly"]


class RuleSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min_adv: float | None = Field(default=None, ge=0)
    min_price: float | None = Field(default=None, ge=0)
    asset_classes: list[Literal["equity", "crypto", "commodity", "bond"]] | None = None
    sectors: list[str] | None = None
    exclude_sectors: list[str] = Field(default_factory=list)
    exchanges: list[str] | None = None
    adv_window_bars: int = Field(default=20, ge=1, le=2520)
    rebalance: Rebalance = "monthly"
    start: date
    end: date | None = None

    @model_validator(mode="after")
    def _window(self) -> Self:
        if self.end is not None and self.end < self.start:
            raise ValueError("end must be on or after start")
        return self

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


def rebalance_dates(start: date, end: date, every: Rebalance) -> list[date]:
    """``start``, then the first day of each week (Monday), month or
    quarter after it, up to ``end``, then ``end``."""
    out = [start]
    if every == "weekly":
        day = start + timedelta(days=7 - start.weekday())
        while day <= end:
            out.append(day)
            day += timedelta(days=7)
    else:
        step = 1 if every == "monthly" else 3
        month = start.month - 1
        if every == "quarterly":
            month -= month % 3
        year = start.year
        while True:
            month += step
            year, month = year + month // 12, month % 12
            day = date(year, month + 1, 1)
            if day > end:
                break
            out.append(day)
    if end != out[-1]:
        out.append(end)
    return out


class RuleProvider(UniverseProvider):
    kind = "rule"
    spec_model = RuleSpec

    def materialize(self, spec: RuleSpec, ctx: RefreshContext) -> Materialized:
        end = spec.end or ctx.as_of
        if end < spec.start:
            raise ValueError(f"the rule starts on {spec.start}, after the refresh date {end}")
        rule = spec.rule()
        dates = rebalance_dates(spec.start, end, spec.rebalance)
        # the last evaluation holds through ``end``; open ended without one
        close = end + timedelta(days=1) if spec.end is not None else None
        opened: dict[str, date] = {}
        spans: list[MembershipSpan] = []
        for day in dates:
            members = set(resolve(ctx.lake, rule, day))
            for ticker in [t for t in opened if t not in members]:
                spans.append(MembershipSpan(ticker, opened.pop(ticker), day))
            for ticker in members:
                opened.setdefault(ticker, day)
        spans += [MembershipSpan(t, start, close) for t, start in opened.items()]
        warnings = [] if spans else ["the rule matched no instrument on any rebalance date"]
        return Materialized(spans=spans, warnings=warnings)

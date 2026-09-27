"""``rule`` universes: point-in-time filters evaluated on the lake.

At each rebalance date between ``start`` and ``end`` (default: the refresh
date) the filters of :class:`~stonks.lab.universe.UniverseRule` pick the
members from what the lake knew on that date: listed and not yet
delisted, average daily dollar volume, last price, asset class, sector
and exchange. The spec is a screen (:mod:`stonks.screener`), so metric
bounds, an order and a top N apply on each date too. A name is a member
from the rebalance date it qualified on until the next rebalance date it
failed. The rule reads only bars and
instrument rows already in the lake, so ingest (or ensure) the candidate
data first.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Literal, Self

from pydantic import model_validator

from stonks.screener.engine import screen_tickers
from stonks.screener.spec import ScreenSpec
from stonks.universes.base import Materialized, MembershipSpan, RefreshContext, UniverseProvider

Rebalance = Literal["weekly", "monthly", "quarterly"]


class RuleSpec(ScreenSpec):
    """A screen (:class:`~stonks.screener.spec.ScreenSpec`: the rule's
    filters, metric bounds, an order and a top N) run at each rebalance
    date from ``start`` to ``end``."""

    rebalance: Rebalance = "monthly"
    start: date
    end: date | None = None

    @model_validator(mode="after")
    def _window(self) -> Self:
        if self.end is not None and self.end < self.start:
            raise ValueError("end must be on or after start")
        return self


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
        dates = rebalance_dates(spec.start, end, spec.rebalance)
        # the last evaluation holds through ``end``; open ended without one
        close = end + timedelta(days=1) if spec.end is not None else None
        opened: dict[str, date] = {}
        spans: list[MembershipSpan] = []
        for day in dates:
            members = set(screen_tickers(ctx.lake, spec, day))
            for ticker in [t for t in opened if t not in members]:
                spans.append(MembershipSpan(ticker, opened.pop(ticker), day))
            for ticker in members:
                opened.setdefault(ticker, day)
        spans += [MembershipSpan(t, start, close) for t, start in opened.items()]
        warnings = [] if spans else ["the rule matched no instrument on any rebalance date"]
        return Materialized(spans=spans, warnings=warnings)

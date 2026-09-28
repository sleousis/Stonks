"""Breadth from daily bars (roadmap 23.14).

- Advances and declines: stocks whose close rose, fell or held against their
  previous close, on the last day.
- Above the 50 and 200 day averages: the share of stocks with enough bars
  whose close is above the mean of their last 50 (or 200) closes.
- New highs and lows: stocks whose close beat every close of the last year
  (``high_low_window`` sessions), or fell under every one.
- Distribution days: sessions in the last ``distribution_window`` where the
  index fell by ``distribution_drop`` or more on higher volume than the day
  before (William O'Neil's sign of big sellers).

Closes are adjusted when the caller passes adjusted closes, so a split is
never a new low. No bar after ``as_of`` is read.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

import pandas as pd
from pydantic import BaseModel, Field

from stonks.breadth.settings import BreadthSettings

Tone = Literal["good", "neutral", "bad"]


class AboveAverage(BaseModel):
    days: int = Field(description="The average's length in sessions.")
    count: int = Field(description="Stocks closing above it.")
    eligible: int = Field(description="Stocks with enough bars to have the average.")
    pct: float | None = Field(description="count / eligible; null when none is eligible.")


class BreadthLine(BaseModel):
    key: Literal["advance_decline", "above_average", "highs_lows", "distribution", "empty"]
    text: str = Field(description="One plain sentence.")
    tone: Tone


class Breadth(BaseModel):
    as_of: date | None = Field(description="The last day with bars; null for an empty lake.")
    members: int = Field(description="Stocks with a close on as_of and the day before.")
    advancers: int
    decliners: int
    unchanged: int
    advance_decline_ratio: float | None = Field(
        description="advancers / decliners; null without decliners."
    )
    above_50: AboveAverage
    above_200: AboveAverage
    new_highs: int
    new_lows: int
    high_low_window: int = Field(description="Sessions in the high and low window.")
    index: str | None = Field(description="The index distribution days are counted on.")
    distribution_days: int | None = Field(description="Null without index bars.")
    distribution_dates: list[date]
    distribution_window: int
    lines: list[BreadthLine] = Field(description="What the numbers mean, in plain words.")


def market_breadth(
    bars: pd.DataFrame,
    index_bars: pd.DataFrame | None,
    *,
    settings: BreadthSettings,
    as_of: date | None = None,
) -> Breadth:
    """Breadth of the stocks in ``bars`` (columns ``ticker``, ``date``,
    ``close``) and distribution days on ``index_bars`` (``date``,
    ``close``, ``volume``), on the last day on or before ``as_of``."""
    wide = _wide(bars, as_of)
    if wide.empty:
        return _empty(settings)
    day = _as_day(wide.index[-1])
    last = wide.iloc[-1]
    prev = wide.ffill().shift(1).iloc[-1]
    counted = last.notna() & prev.notna()
    change = (last - prev)[counted]
    advancers = int((change > 0).sum())
    decliners = int((change < 0).sum())
    unchanged = int((change == 0).sum())

    above = {n: [0, 0] for n in (50, 200)}
    highs = lows = 0
    for ticker in wide.columns[last.notna().to_numpy()]:
        closes = wide[ticker].dropna().to_numpy(dtype=float)
        now = closes[-1]
        for n, acc in above.items():
            if len(closes) >= n:
                acc[1] += 1
                acc[0] += int(now > closes[-n:].mean())
        if len(closes) >= settings.min_history:
            prior = closes[-settings.high_low_window : -1]
            highs += int(now > prior.max())
            lows += int(now < prior.min())

    dist_dates = _distribution(index_bars, day, settings) if settings.index else None
    result = Breadth(
        as_of=day,
        members=int(counted.sum()),
        advancers=advancers,
        decliners=decliners,
        unchanged=unchanged,
        advance_decline_ratio=advancers / decliners if decliners else None,
        above_50=_above(50, *above[50]),
        above_200=_above(200, *above[200]),
        new_highs=highs,
        new_lows=lows,
        high_low_window=settings.high_low_window,
        index=settings.index,
        distribution_days=None if dist_dates is None else len(dist_dates),
        distribution_dates=dist_dates or [],
        distribution_window=settings.distribution_window,
        lines=[],
    )
    return result.model_copy(update={"lines": describe(result)})


def _wide(bars: pd.DataFrame, as_of: date | None) -> pd.DataFrame:
    if bars.empty:
        return pd.DataFrame()
    frame = bars[["ticker", "date", "close"]].dropna()
    frame = frame.assign(date=[_as_day(d) for d in frame["date"]])
    if as_of is not None:
        frame = frame[frame["date"] <= as_of]
    if frame.empty:
        return pd.DataFrame()
    return frame.pivot_table(index="date", columns="ticker", values="close", aggfunc="last")


def _as_day(value: object) -> date:
    """A calendar day from a date, a datetime or a pandas timestamp."""
    stamp = pd.Timestamp(value)  # type: ignore[arg-type]
    return date(stamp.year, stamp.month, stamp.day)


def _above(days: int, count: int, eligible: int) -> AboveAverage:
    return AboveAverage(
        days=days, count=count, eligible=eligible, pct=count / eligible if eligible else None
    )


def _distribution(
    index_bars: pd.DataFrame | None, day: date, settings: BreadthSettings
) -> list[date] | None:
    if index_bars is None or index_bars.empty:
        return None
    frame = index_bars[["date", "close", "volume"]].copy()
    frame["date"] = [_as_day(d) for d in frame["date"]]
    kept: pd.DataFrame = frame.loc[frame["date"] <= day]
    frame = kept.sort_values(by="date").reset_index(drop=True)
    if len(frame) < 2:
        return None
    fell = frame["close"].pct_change(fill_method=None) <= -settings.distribution_drop
    heavier = frame["volume"] > frame["volume"].shift(1)
    recent = frame.tail(settings.distribution_window)
    hits = (fell & heavier).loc[recent.index]
    return [d for d, hit in zip(recent["date"], hits, strict=True) if bool(hit)]


def _empty(settings: BreadthSettings) -> Breadth:
    return Breadth(
        as_of=None,
        members=0,
        advancers=0,
        decliners=0,
        unchanged=0,
        advance_decline_ratio=None,
        above_50=_above(50, 0, 0),
        above_200=_above(200, 0, 0),
        new_highs=0,
        new_lows=0,
        high_low_window=settings.high_low_window,
        index=settings.index,
        distribution_days=None,
        distribution_dates=[],
        distribution_window=settings.distribution_window,
        lines=[
            BreadthLine(
                key="empty",
                text="No price data yet. Ingest daily prices to see how the market is doing.",
                tone="neutral",
            )
        ],
    )


def describe(b: Breadth) -> list[BreadthLine]:
    """Plain sentences for each number, with a tone for the colour."""
    lines: list[BreadthLine] = []
    text: str
    tone: Tone
    if b.members:
        up, down = b.advancers, b.decliners
        if up > down * 1.1:
            text, tone = f"More stocks rose than fell: {up} up, {down} down.", "good"
        elif down > up * 1.1:
            text, tone = f"More stocks fell than rose: {down} down, {up} up.", "bad"
        else:
            text, tone = f"Rises and falls were about even: {up} up, {down} down.", "neutral"
        lines.append(BreadthLine(key="advance_decline", text=text, tone=tone))
    long = b.above_200 if b.above_200.pct is not None else b.above_50
    if long.pct is not None:
        pct = f"{long.pct:.0%}"
        if long.pct >= 0.6:
            text, tone = f"{pct} of stocks are above their {long.days} day average. Most are in uptrends.", "good"  # fmt: skip
        elif long.pct >= 0.4:
            text, tone = f"{pct} of stocks are above their {long.days} day average. The market is mixed.", "neutral"  # fmt: skip
        else:
            text, tone = f"Only {pct} of stocks are above their {long.days} day average. Most are in downtrends.", "bad"  # fmt: skip
        lines.append(BreadthLine(key="above_average", text=text, tone=tone))
    if b.members:
        hi, lo = b.new_highs, b.new_lows
        text = f"{_count(hi)} at a one year high and {_count(lo)} at a one year low."
        tone = "good" if hi > lo else "bad" if lo > hi else "neutral"
        lines.append(BreadthLine(key="highs_lows", text=text, tone=tone))
    if b.distribution_days is not None and b.index:
        n, name = b.distribution_days, b.index.split(".")[0]
        days = f"{n} distribution day{'' if n == 1 else 's'}"
        window = f"in the last {b.distribution_window} sessions"
        if n >= 5:
            text, tone = f"{days} on {name} {window}: big sellers are active.", "bad"
        elif n >= 3:
            text, tone = f"{days} on {name} {window}: some selling pressure.", "neutral"
        else:
            text, tone = f"{days} on {name} {window}: little heavy selling.", "good"
        lines.append(BreadthLine(key="distribution", text=text, tone=tone))
    return lines


def _count(n: int) -> str:
    return f"{n} stock{'' if n == 1 else 's'}"


__all__ = ["AboveAverage", "Breadth", "BreadthLine", "describe", "market_breadth"]

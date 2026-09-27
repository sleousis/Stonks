"""Fundamentals scores as factors (roadmap 22.8).

The value, quality and forensic scores of
:mod:`stonks.features.fundamentals`, read point in time exactly as the
fundamentals strategies read them: :class:`QuantValue` and
:class:`QualityValue` metrics through a
:class:`~stonks.store.pit.PointInTimeLake` view at each date, so a filing
counts from the day after it and a share count only after its publication
lag (P12). Equities only.

Each date is scored on its own (statements change a few times a year and
the scores need whole annual reports), so a tear sheet passes its sampled
dates rather than every bar.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd

from stonks.core.interval import Interval
from stonks.core.timeutil import day_end, day_start
from stonks.factors.base import Factor, FactorKind
from stonks.factors.engine import PanelRequest, membership_spans, read_bars
from stonks.store.pit import PitSession, PointInTimeLake

__all__ = ["FundamentalFactor", "factors"]

_TABLES = ("income_statement", "balance_sheet", "cash_flow_statement", "shares_outstanding")

Scorer = Callable[[], Any]


def _quant_value() -> Any:
    from stonks.strategies.examples.quant_value import QuantValue

    return QuantValue({})


def _quality_value() -> Any:
    from stonks.strategies.examples.quality_value import QualityValue

    return QualityValue({})


class FundamentalFactor(Factor):
    """One metric of a fundamentals strategy's ``extract_features``."""

    kind: FactorKind = "fundamental"
    tables = _TABLES
    asset_classes = ("equity",)
    whole_window = False

    def __init__(
        self,
        id: str,
        metric: str,
        *,
        description: str,
        family: str,
        direction: int,
        hypothesis: str,
        scorer: Scorer = _quant_value,
    ) -> None:
        self.id = id
        self.metric = metric
        self.description = description
        self.family = family
        self.direction = direction
        self.hypothesis = hypothesis
        self._scorer = scorer

    def _values(self, scorer: Any, view: Any, tickers: Sequence[str], as_of: datetime) -> dict:
        out: dict[str, float] = {}
        for ticker in tickers:
            features = scorer.extract_features(ticker, as_of, view)
            value = features.values.get(self.metric)
            if value is not None and math.isfinite(float(value)):
                out[ticker] = float(value)
        return out

    def values_at(
        self,
        lake: Any,
        tickers: Sequence[str],
        as_of: datetime,
        interval: Interval = Interval.DAY_1,
    ) -> dict[str, float]:
        view = lake if isinstance(lake, PointInTimeLake) else PitSession(lake).at(as_of)
        return self._values(self._scorer(), view, tickers, as_of)

    def panel(
        self, lake: Any, request: PanelRequest, dates: pd.DatetimeIndex | None = None
    ) -> pd.DataFrame:
        if dates is None:
            bars = read_bars(
                lake,
                request.universe,
                request.interval,
                day_start(request.start),
                day_end(request.end),
            )
            dates = pd.DatetimeIndex(sorted(pd.to_datetime(bars["timestamp"]).unique()))
        columns = list(request.universe)
        values = np.full((len(dates), len(columns)), np.nan)
        session = PitSession(lake)
        scorer = self._scorer()
        members = _members(request, dates)
        for i, stamp in enumerate(dates):
            at = stamp.to_pydatetime()
            names = [t for t in columns if members is None or members.get((i, t), False)]
            got = self._values(scorer, session.at(at), names, at)
            for j, ticker in enumerate(columns):
                if ticker in got:
                    values[i, j] = got[ticker]
        return pd.DataFrame(
            values, index=pd.DatetimeIndex(dates, name="timestamp"), columns=columns
        )


def _members(request: PanelRequest, dates: pd.DatetimeIndex) -> dict | None:
    if request.membership is None:
        return None
    out: dict[tuple[int, str], bool] = {}
    days = pd.DatetimeIndex(np.asarray(dates, dtype="datetime64[D]"))
    for ticker, lo, hi in membership_spans(request.membership):
        for i, day in enumerate(days):
            if lo <= day < hi:  # end_date is exclusive
                out[(i, ticker)] = True
    return out


def factors() -> list[Factor]:
    def f(
        id: str, metric: str, family: str, direction: int, what: str, why: str, **kw: Any
    ) -> FundamentalFactor:
        return FundamentalFactor(
            id, metric, description=what, family=family, direction=direction, hypothesis=why, **kw
        )

    return [
        f(
            "ebit_tev",
            "ebit_tev",
            "value",
            1,
            "operating earnings over total enterprise value",
            "Cheap firms relative to their operating earnings are priced for too little "
            "growth and rerate (Gray and Carlisle). Fails when cheapness reflects real decline.",
        ),
        f(
            "book_to_market",
            "bm",
            "value",
            1,
            "book equity over market cap",
            "The classic value premium (Fama and French): high book-to-market firms earn "
            "more, as pay for distress risk or as a behavioural overreaction. Fails in "
            "long growth-led markets.",
        ),
        f(
            "return_on_capital",
            "roc",
            "quality",
            1,
            "operating earnings over tangible capital",
            "Firms that earn a high return on capital keep doing so and are underpriced "
            "(Greenblatt). Fails when high returns attract competition.",
        ),
        f(
            "piotroski_f",
            "f_score",
            "quality",
            1,
            "Piotroski F-score, 0 to 9",
            "Among cheap stocks, those with improving profitability, liquidity and "
            "efficiency outperform (Piotroski 2000). Fails in junk rallies.",
        ),
        f(
            "financial_strength",
            "fs",
            "quality",
            1,
            "Gray and Carlisle financial strength score, 0 to 10",
            "Financially strong firms are less likely to fail and are underpriced relative "
            "to weak ones. Fails in junk rallies.",
        ),
        f(
            "altman_z",
            "altman_z",
            "quality",
            1,
            "Altman Z-score (distance from bankruptcy)",
            "Firms far from distress outperform distressed ones (the distress anomaly). "
            "Fails in rebounds from recessions, when distressed names rally.",
        ),
        f(
            "accruals",
            "sta",
            "forensic",
            -1,
            "scaled total accruals",
            "Earnings built on accruals rather than cash reverse, and investors fixate on "
            "earnings, so high-accrual firms underperform (Sloan 1996).",
        ),
        f(
            "net_operating_assets",
            "snoa",
            "forensic",
            -1,
            "scaled net operating assets",
            "Bloated balance sheets signal past earnings that outran cash flow, so high "
            "net operating assets predict lower returns (Hirshleifer and others 2004).",
        ),
        f(
            "beneish_m",
            "beneish_m",
            "forensic",
            -1,
            "Beneish M-score (earnings manipulation)",
            "Firms whose statements look manipulated underperform when the truth comes "
            "out (Beneish 1999).",
        ),
        f(
            "manipulation_probability",
            "pman",
            "forensic",
            -1,
            "probability of manipulation from the M-score",
            "The M-score as a probability: higher means more likely manipulation and lower "
            "future returns.",
        ),
        f(
            "quality_value",
            "score",
            "quality",
            1,
            "QualityValue composite of earnings and cash yield, ROE, margin and leverage",
            "Cheap, profitable, lightly levered firms outperform: value and quality premia "
            "together. Fails in speculative growth markets.",
            scorer=_quality_value,
        ),
    ]

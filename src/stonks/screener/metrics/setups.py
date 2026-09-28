"""Chart setups as screener metrics (roadmap 23.13): one metric per factor
of the ``setups`` set (:mod:`stonks.factors.library.setups`), valued as the
factor is at the close of the screen date. ``+1`` is a bullish form, ``-1``
a bearish one, ``0`` none, so a screen can ask for ``setup_nr7 >= 1``.

The factor reads the bars known on the date (P12), the same values its
tear sheet and the factor bench test."""

from __future__ import annotations

from datetime import datetime, time
from typing import ClassVar

from stonks.screener.data import ScreenData
from stonks.screener.metrics.base import ScreenMetric


class _SetupMetric(ScreenMetric):
    factor_id: ClassVar[str]
    group = "price"
    unit = "ratio"

    def compute(self, data: ScreenData) -> dict[str, float]:
        from stonks.factors.registry import get_factor

        if not data.tickers:
            return {}
        when = datetime.combine(data.as_of, time())
        return get_factor(self.factor_id).values_at(data.lake, data.tickers, when)


class Engulfing(_SetupMetric):
    id = "setup_engulfing"
    factor_id = "engulfing"
    label = "Engulfing candle"
    description = "+1 bullish engulfing, -1 bearish engulfing, 0 none, on the date."


class Hammer(_SetupMetric):
    id = "setup_hammer"
    factor_id = "hammer"
    label = "Hammer or shooting star"
    description = "+1 hammer after a decline, -1 shooting star after a rise, 0 none."


class Star(_SetupMetric):
    id = "setup_star"
    factor_id = "star"
    label = "Morning or evening star"
    description = "+1 morning star, -1 evening star, 0 none (three candles to the date)."


class InsideBar(_SetupMetric):
    id = "setup_inside_bar"
    factor_id = "inside_bar"
    label = "Inside bar"
    description = "An inside bar on the date, +1 in an uptrend and -1 in a downtrend."


class Nr7(_SetupMetric):
    id = "setup_nr7"
    factor_id = "nr7"
    label = "NR7"
    description = "The narrowest range of 7 bars, +1 in an uptrend and -1 in a downtrend."


class Vcp(_SetupMetric):
    id = "setup_vcp"
    factor_id = "vcp"
    label = "Volatility contraction"
    description = "1 when ranges tighten three times in an uptrend near the 52-week high."


class Squeeze(_SetupMetric):
    id = "setup_squeeze"
    factor_id = "squeeze"
    label = "Squeeze"
    description = "Bollinger bands inside the Keltner channel, signed by the 20-bar trend."

"""Named chart setups as factors (roadmap 23.13), so they can be tested
honestly: tear sheets, the factor bench and the survival suite, instead of
folklore.

Each setup is a formula in the expression language, point in time like any
other factor. Directional setups are signed: ``+1`` on a bullish form,
``-1`` on a bearish one, ``0`` otherwise. Setups with no direction of their
own (NR7, the inside bar, the squeeze) take the sign of the trend, the close
against its moving average, the way traders read them. The volatility
contraction pattern is ``1`` or ``0``.

The evidence for most of these is thin. Marshall, Young and Rose (2006,
Journal of Banking and Finance 30(8)) found no value in candlestick
patterns on Dow stocks. Expect most to show no edge after costs, which is
itself worth knowing. Every setup is also a screener metric
(``setup_<id>``, :mod:`stonks.screener.metrics.setups`).
"""

from __future__ import annotations

from stonks.factors.base import ExpressionFactor, Factor

_BODY = "Abs($close - $open)"
_LOWER = "(Less($open, $close) - $low)"
_UPPER = "($high - Greater($open, $close))"
_TRUE_RANGE = "(Greater($high, Ref($close, 1)) - Less($low, Ref($close, 1)))"
_TREND_50 = "Sign($close - Mean($close, 50))"
_TREND_20 = "Sign($close - Mean($close, 20))"

_BULL_ENGULF = (
    "(Ref($close, 1) < Ref($open, 1)) & ($close > $open)"
    " & ($open <= Ref($close, 1)) & ($close >= Ref($open, 1))"
)
_BEAR_ENGULF = (
    "(Ref($close, 1) > Ref($open, 1)) & ($close < $open)"
    " & ($open >= Ref($close, 1)) & ($close <= Ref($open, 1))"
)
_HAMMER = f"({_LOWER} >= 2 * {_BODY}) & ({_UPPER} <= {_BODY}) & ($close < Mean($close, 10))"
_SHOOTING_STAR = f"({_UPPER} >= 2 * {_BODY}) & ({_LOWER} <= {_BODY}) & ($close > Mean($close, 10))"
_SMALL_MIDDLE = "(Abs(Ref($close, 1) - Ref($open, 1)) < 0.3 * Abs(Ref($close, 2) - Ref($open, 2)))"
_FIRST_MID = "(Ref($open, 2) + Ref($close, 2)) / 2"
_MORNING = (
    f"(Ref($close, 2) < Ref($open, 2)) & {_SMALL_MIDDLE}"
    f" & ($close > $open) & ($close > {_FIRST_MID})"
)
_EVENING = (
    f"(Ref($close, 2) > Ref($open, 2)) & {_SMALL_MIDDLE}"
    f" & ($close < $open) & ($close < {_FIRST_MID})"
)
_RANGE_NOW = "(Max($high, 10) - Min($low, 10))"
_RANGE_BEFORE = "(Ref(Max($high, 10), 10) - Ref(Min($low, 10), 10))"
_RANGE_FIRST = "(Ref(Max($high, 20), 20) - Ref(Min($low, 20), 20))"
_VCP = (
    f"({_RANGE_NOW} < {_RANGE_BEFORE}) & ({_RANGE_BEFORE} < {_RANGE_FIRST})"
    " & ($close > Mean($close, 50)) & (Mean($close, 50) > Mean($close, 200))"
    " & ($close >= 0.75 * Max($high, 252))"
)

_WEAK = (
    " Published tests find little or no edge in candlestick patterns after costs "
    "(Marshall, Young and Rose 2006), so treat a good tear sheet with suspicion."
)


def factors() -> list[Factor]:
    def setup(id: str, expression: str, what: str, why: str) -> ExpressionFactor:
        return ExpressionFactor(id, expression, description=what, family="setup", hypothesis=why)

    return [
        setup(
            "engulfing",
            f"({_BULL_ENGULF}) - ({_BEAR_ENGULF})",
            "engulfing candle: +1 bullish, -1 bearish, 0 none",
            "A candle whose body swallows the previous one against its direction is read "
            "as a reversal of short-term control." + _WEAK,
        ),
        setup(
            "hammer",
            f"({_HAMMER}) - ({_SHOOTING_STAR})",
            "hammer after a decline +1, shooting star after a rise -1, 0 none",
            "A long lower shadow after a decline shows buyers rejected lower prices, and "
            "a long upper shadow after a rise shows sellers rejected higher ones." + _WEAK,
        ),
        setup(
            "star",
            f"({_MORNING}) - ({_EVENING})",
            "morning star +1, evening star -1, 0 none",
            "Three candles: a strong move, a small pause and a strong move back past the "
            "middle of the first, read as a turn." + _WEAK,
        ),
        setup(
            "inside_bar",
            f"If(($high < Ref($high, 1)) & ($low > Ref($low, 1)), {_TREND_50}, 0)",
            "inside bar, signed by the 50-bar trend",
            "A bar inside the previous one is a pause, and traders expect the break to "
            "follow the trend. Fails in choppy markets, where breaks fail both ways.",
        ),
        setup(
            "nr7",
            f"If(($high - $low) <= Min($high - $low, 7), {_TREND_50}, 0)",
            "narrowest range of the last 7 bars (Crabel's NR7), signed by the 50-bar trend",
            "A range contraction comes before a range expansion (Crabel 1990), and the "
            "expansion should follow the trend. Fails when the expansion goes against it.",
        ),
        setup(
            "vcp",
            _VCP,
            "volatility contraction pattern in an uptrend near the 52-week high: 1 or 0",
            "Successively tighter bases in a leader near its high show supply drying up "
            "before a breakout (Minervini). Fails in weak markets, where breakouts fail.",
        ),
        setup(
            "squeeze",
            f"If(2 * Std($close, 20) < 1.5 * Mean({_TRUE_RANGE}, 20), {_TREND_20}, 0)",
            "Bollinger bands inside the Keltner channel (the squeeze), signed by the 20-bar trend",
            "When the bands sit inside the channel, volatility is compressed and a move "
            "should follow in the trend's direction (Carter's TTM squeeze). Fails when "
            "the release goes against the trend.",
        ),
    ]

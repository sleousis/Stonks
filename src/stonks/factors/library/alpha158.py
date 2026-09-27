"""An Alpha158-style factor set (roadmap 22.8), ported from Qlib's
``Alpha158`` handler (MIT licence, Microsoft).

157 price and volume features: nine candlestick (Kbar) shapes, three price
ratios, and 29 rolling features at windows of 5, 10, 20, 30 and 60 bars.
Every one is a ratio or a rank, so it compares across tickers and is exact
under the as-of adjustment (:mod:`stonks.factors.expression`).

Differences from Qlib:

- no ``VWAP0``: the lake's bars carry no VWAP field;
- no ``VOLUME0``: at window 0 it is always 1;
- ``CORR`` uses ``Log($volume)`` instead of ``Log($volume + 1)``: adding a
  constant to a volume level would depend on later splits. A zero volume
  gives an empty value instead;
- the label is the next-open return (:func:`next_open_label`), not Qlib's
  next-close one, so it matches how the backtest fills.

These are research features with no single hypothesis each: the sign is
``+1`` and a tear sheet shows which way each one ranks returns.
"""

from __future__ import annotations

from stonks.factors.base import ExpressionFactor, Factor

WINDOWS = (5, 10, 20, 30, 60)

_EPS = "1e-12"
_RANGE = f"($high-$low+{_EPS})"

KBAR: dict[str, tuple[str, str]] = {
    "KMID": ("($close-$open)/$open", "body of the bar over the open"),
    "KLEN": ("($high-$low)/$open", "range of the bar over the open"),
    "KMID2": (f"($close-$open)/{_RANGE}", "body over the range"),
    "KUP": ("($high-Greater($open, $close))/$open", "upper shadow over the open"),
    "KUP2": (f"($high-Greater($open, $close))/{_RANGE}", "upper shadow over the range"),
    "KLOW": ("(Less($open, $close)-$low)/$open", "lower shadow over the open"),
    "KLOW2": (f"(Less($open, $close)-$low)/{_RANGE}", "lower shadow over the range"),
    "KSFT": ("(2*$close-$high-$low)/$open", "close position in the bar over the open"),
    "KSFT2": (f"(2*$close-$high-$low)/{_RANGE}", "close position in the bar over the range"),
}

PRICE: dict[str, tuple[str, str]] = {
    "OPEN0": ("$open/$close", "open over close"),
    "HIGH0": ("$high/$close", "high over close"),
    "LOW0": ("$low/$close", "low over close"),
}

_UP = "Greater($close-Ref($close, 1), 0)"
_DOWN = "Greater(Ref($close, 1)-$close, 0)"
_MOVE = "Abs($close-Ref($close, 1))"
_VUP = "Greater($volume-Ref($volume, 1), 0)"
_VDOWN = "Greater(Ref($volume, 1)-$volume, 0)"
_VMOVE = "Abs($volume-Ref($volume, 1))"
_WRET = "Abs($close/Ref($close, 1)-1)*$volume"

#: name -> (template over ``{d}``, family, description)
ROLLING: dict[str, tuple[str, str, str]] = {
    "ROC": ("Ref($close, {d})/$close", "price", "close {d} bars ago over today's"),
    "MA": ("Mean($close, {d})/$close", "price", "{d}-bar mean close over today's"),
    "STD": ("Std($close, {d})/$close", "volatility", "{d}-bar close deviation over today's"),
    "BETA": ("Slope($close, {d})/$close", "trend", "{d}-bar slope of the close over today's"),
    "RSQR": ("Rsquare($close, {d})", "trend", "R squared of the {d}-bar close trend"),
    "RESI": ("Resi($close, {d})/$close", "trend", "distance from the {d}-bar trend line"),
    "MAX": ("Max($high, {d})/$close", "price", "{d}-bar high over the close"),
    "MIN": ("Min($low, {d})/$close", "price", "{d}-bar low over the close"),
    "QTLU": ("Quantile($close, {d}, 0.8)/$close", "price", "{d}-bar 80th percentile close"),
    "QTLD": ("Quantile($close, {d}, 0.2)/$close", "price", "{d}-bar 20th percentile close"),
    "RANK": ("Rank($close, {d})", "price", "percentile of today's close in {d} bars"),
    "RSV": (
        f"($close-Min($low, {{d}}))/(Max($high, {{d}})-Min($low, {{d}})+{_EPS})",
        "price",
        "close position in the {d}-bar range",
    ),
    "IMAX": ("IdxMax($high, {d})/{d}", "price", "age of the {d}-bar high"),
    "IMIN": ("IdxMin($low, {d})/{d}", "price", "age of the {d}-bar low"),
    "IMXD": (
        "(IdxMax($high, {d})-IdxMin($low, {d}))/{d}",
        "price",
        "bars between the {d}-bar high and low",
    ),
    "CORR": ("Corr($close, Log($volume), {d})", "volume", "{d}-bar price-volume correlation"),
    "CORD": (
        "Corr($close/Ref($close, 1), Log($volume/Ref($volume, 1)+1), {d})",
        "volume",
        "{d}-bar correlation of price and volume changes",
    ),
    "CNTP": ("Mean($close>Ref($close, 1), {d})", "momentum", "share of up bars in {d}"),
    "CNTN": ("Mean($close<Ref($close, 1), {d})", "momentum", "share of down bars in {d}"),
    "CNTD": (
        "Mean($close>Ref($close, 1), {d})-Mean($close<Ref($close, 1), {d})",
        "momentum",
        "up share minus down share in {d} bars",
    ),
    "SUMP": (
        f"Sum({_UP}, {{d}})/(Sum({_MOVE}, {{d}})+{_EPS})",
        "momentum",
        "share of {d}-bar movement that was up (an RSI)",
    ),
    "SUMN": (
        f"Sum({_DOWN}, {{d}})/(Sum({_MOVE}, {{d}})+{_EPS})",
        "momentum",
        "share of {d}-bar movement that was down",
    ),
    "SUMD": (
        f"(Sum({_UP}, {{d}})-Sum({_DOWN}, {{d}}))/(Sum({_MOVE}, {{d}})+{_EPS})",
        "momentum",
        "up minus down movement over {d} bars",
    ),
    "VMA": (f"Mean($volume, {{d}})/($volume+{_EPS})", "volume", "{d}-bar mean volume over today's"),
    "VSTD": (f"Std($volume, {{d}})/($volume+{_EPS})", "volume", "{d}-bar volume deviation"),
    "WVMA": (
        f"Std({_WRET}, {{d}})/(Mean({_WRET}, {{d}})+{_EPS})",
        "volume",
        "{d}-bar variation of volume-weighted moves",
    ),
    "VSUMP": (
        f"Sum({_VUP}, {{d}})/(Sum({_VMOVE}, {{d}})+{_EPS})",
        "volume",
        "share of {d}-bar volume change that was up",
    ),
    "VSUMN": (
        f"Sum({_VDOWN}, {{d}})/(Sum({_VMOVE}, {{d}})+{_EPS})",
        "volume",
        "share of {d}-bar volume change that was down",
    ),
    "VSUMD": (
        f"(Sum({_VUP}, {{d}})-Sum({_VDOWN}, {{d}}))/(Sum({_VMOVE}, {{d}})+{_EPS})",
        "volume",
        "up minus down volume change over {d} bars",
    ),
}

_HYPOTHESIS = (
    "An Alpha158 research feature (Qlib): price and volume shape that a model or a "
    "tear sheet may find ranks next-open returns. No single-feature edge is claimed."
)


def factors() -> list[Factor]:
    out: list[Factor] = []
    for name, (expr, what) in KBAR.items():
        out.append(
            ExpressionFactor(name, expr, description=what, family="kbar", hypothesis=_HYPOTHESIS)
        )
    for name, (expr, what) in PRICE.items():
        out.append(
            ExpressionFactor(name, expr, description=what, family="price", hypothesis=_HYPOTHESIS)
        )
    for name, (template, family, what) in ROLLING.items():
        for d in WINDOWS:
            out.append(
                ExpressionFactor(
                    f"{name}{d}",
                    template.format(d=d),
                    description=what.format(d=d),
                    family=family,
                    hypothesis=_HYPOTHESIS,
                )
            )
    return out

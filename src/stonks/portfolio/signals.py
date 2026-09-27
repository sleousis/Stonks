"""Signal normalisation: raw strategy scores onto one comparable scale
(BL-08, principles P3 and P32).

Raw ``estimate_return`` values are in each strategy's own units
(``BuyAndHold`` says 1.0 for everything it likes; a quality screen says
0.05), so they can't be compared or summed. :func:`normalize` maps each
strategy's scores independently:

- ``"zscore"``: cross-sectional z-score within the strategy, winsorised at
  +/-3 and re-standardised.
- ``"rank"``: cross-sectional percentile in ``(0, 1]``.
- ``"signed_rank"``: the percentile within each sign, so a positive score
  stays positive (in ``(0, 1]``) and a negative one negative (in
  ``[-1, 0)``). Long/short books use it where a z-score would short the
  weakest of names the strategy expects to rise (BE-15).
- ``"forecast"``: Carver's forecast scaling, ``f = raw * scalar`` capped at
  +/-20, where ``scalar = 10 / mean|raw|`` is estimated in the lab
  (:func:`estimate_forecast_scalar`) and passed in
  :class:`SignalContext`. Without a stored scalar, it is estimated from the
  current cross section.
- ``"alpha"``: Grinold-Kahn ``alpha_i = IC_s * sigma_i * z_i`` with the
  strategy's information coefficient (default 0.02) and annual sigma.
- ``"raw"``: pass-through (for :class:`single_winner`).

A cross section needs at least 3 names; a strategy with fewer, or with a
constant cross section (every name equally liked, RS-06), gets a sign-only
conviction of 1.0 on the method's scale (1.0 for z-score, rank and
the ``z`` of alpha; 10, the average forecast, for ``forecast``). In
long-only mode negative outputs are clipped to 0. Non-finite scores are
dropped. Methods live in a registry; :func:`register_normalizer` adds one.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field

import pandas as pd

from stonks.features.cross_section import cs_rank, cs_zscore

FORECAST_TARGET = 10.0  # Carver: average absolute forecast
FORECAST_CAP = 20.0
DEFAULT_IC = 0.02
MIN_CROSS_SECTION = 3
ZSCORE_WINSOR = 3.0


@dataclass(frozen=True)
class SignalContext:
    """Per-strategy calibration from the lab and per-ticker data the
    methods may need."""

    forecast_scalars: Mapping[str, float] = field(default_factory=dict)
    ics: Mapping[str, float] = field(default_factory=dict)
    vols_annual: Mapping[str, float] = field(default_factory=dict)


Normalizer = Callable[[str, pd.Series, SignalContext], pd.Series]
_NORMALIZERS: dict[str, Normalizer] = {}


def register_normalizer(name: str) -> Callable[[Normalizer], Normalizer]:
    def decorate(fn: Normalizer) -> Normalizer:
        _NORMALIZERS[name] = fn
        return fn

    return decorate


def normalizer_names() -> list[str]:
    return sorted(_NORMALIZERS)


def _sign(s: pd.Series, scale: float = 1.0) -> pd.Series:
    return s.map(lambda v: scale * ((v > 0) - (v < 0))).astype(float)


@register_normalizer("raw")
def _raw(strategy_id: str, s: pd.Series, ctx: SignalContext) -> pd.Series:
    return s


def _constant(s: pd.Series) -> bool:
    return bool((s == s.iloc[0]).all())


@register_normalizer("zscore")
def _zscore(strategy_id: str, s: pd.Series, ctx: SignalContext) -> pd.Series:
    if len(s) < MIN_CROSS_SECTION or _constant(s):
        return _sign(s)
    return cs_zscore(s, winsor=ZSCORE_WINSOR)


@register_normalizer("rank")
def _rank(strategy_id: str, s: pd.Series, ctx: SignalContext) -> pd.Series:
    if len(s) < MIN_CROSS_SECTION:
        return _sign(s)
    return cs_rank(s)


@register_normalizer("signed_rank")
def _signed_rank(strategy_id: str, s: pd.Series, ctx: SignalContext) -> pd.Series:
    out = pd.Series(0.0, index=s.index)
    for side, sign in ((s[s > 0], 1.0), (-s[s < 0], -1.0)):
        if side.empty:
            continue
        ranked = cs_rank(side) if len(side) >= MIN_CROSS_SECTION else pd.Series(1.0, side.index)
        out.loc[side.index] = sign * ranked.astype(float)
    return out


def estimate_forecast_scalar(raw_history: Iterable[float]) -> float:
    """Carver's forecast scalar ``10 / mean|raw|`` over a strategy's past raw
    scores (pooled across instruments and dates). The lab estimates it and
    stores it with the strategy."""
    values = [abs(float(v)) for v in raw_history if v is not None and math.isfinite(v)]
    mean_abs = sum(values) / len(values) if values else 0.0
    if mean_abs <= 0:
        raise ValueError("cannot scale forecasts whose mean absolute value is 0")
    return FORECAST_TARGET / mean_abs


@register_normalizer("forecast")
def _forecast(strategy_id: str, s: pd.Series, ctx: SignalContext) -> pd.Series:
    scalar = ctx.forecast_scalars.get(strategy_id)
    if scalar is None:
        if len(s) < MIN_CROSS_SECTION or not (s.abs() > 0).any():
            return _sign(s, FORECAST_TARGET)
        scalar = estimate_forecast_scalar(s)
    return (s * float(scalar)).clip(lower=-FORECAST_CAP, upper=FORECAST_CAP)


@register_normalizer("alpha")
def _alpha(strategy_id: str, s: pd.Series, ctx: SignalContext) -> pd.Series:
    sigma = pd.Series({t: ctx.vols_annual.get(t) for t in s.index}, dtype=float)
    usable = sigma.notna() & (sigma > 0) & sigma.map(math.isfinite)
    s, sigma = s[usable], sigma[usable]
    z = _sign(s) if len(s) < MIN_CROSS_SECTION else cs_zscore(s, winsor=ZSCORE_WINSOR)
    ic = float(ctx.ics.get(strategy_id, DEFAULT_IC))
    return ic * sigma * z


def normalize(
    raw: Mapping[str, Mapping[str, float]],
    method: str = "zscore",
    *,
    long_only: bool = True,
    context: SignalContext | None = None,
) -> dict[str, dict[str, float]]:
    """Normalise every strategy's scores with ``method`` (see module doc).
    Returns ``strategy_id -> ticker -> normalised score``."""
    fn = _NORMALIZERS.get(method)
    if fn is None:
        raise ValueError(f"unknown normalisation {method!r}; choose one of {normalizer_names()}")
    if method == "raw":
        return {sid: dict(scores) for sid, scores in raw.items()}
    ctx = context or SignalContext()
    out: dict[str, dict[str, float]] = {}
    for sid, scores in raw.items():
        s = pd.Series(
            {t: float(v) for t, v in scores.items() if v is not None and math.isfinite(v)},
            dtype=float,
        )
        if s.empty:
            out[sid] = {}
            continue
        normalised = fn(sid, s, ctx)
        if long_only:
            normalised = normalised.clip(lower=0.0)
        out[sid] = {t: float(v) for t, v in normalised.items() if math.isfinite(v)}
    return out

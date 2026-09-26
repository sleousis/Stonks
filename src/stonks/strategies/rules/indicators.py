"""The closed indicator registry behind rule specs.

Every indicator is a pure function of a bar window (oldest first, the
last row is the current bar) returning a float series aligned to it.
Callers only ever pass bars with ``timestamp <= as_of``, and no function
here shifts data backwards, so nothing can read the future. Lookbacks are
bar counts. Recursive indicators (EMA, RSI) are warmed up over a fixed
multiple of their period, so their value depends only on that window.
"""

from __future__ import annotations

from collections.abc import Callable

import pandas as pd

from stonks.features.indicators import efficiency_ratio, kama
from stonks.features.library import rolling_zscore, rsi, trailing_return
from stonks.strategies.rules.spec import (
    AtrIndicator,
    CloseIndicator,
    DonchianHighIndicator,
    DonchianLowIndicator,
    EfficiencyRatioIndicator,
    EmaIndicator,
    KamaIndicator,
    RocIndicator,
    RsiIndicator,
    SmaIndicator,
    VolumeIndicator,
    ZScoreIndicator,
)

#: Warm-up multiple for recursive (exponentially smoothed) indicators.
RECURSIVE_WARMUP = 4


def _col(bars: pd.DataFrame, name: str) -> pd.Series:
    return bars[name].astype(float).reset_index(drop=True)


def _close(ind: CloseIndicator, bars: pd.DataFrame) -> pd.Series:
    return _col(bars, "close")


def _volume(ind: VolumeIndicator, bars: pd.DataFrame) -> pd.Series:
    return _col(bars, "volume")


def _sma(ind: SmaIndicator, bars: pd.DataFrame) -> pd.Series:
    return _col(bars, ind.source).rolling(ind.period, min_periods=ind.period).mean()


def _ema(ind: EmaIndicator, bars: pd.DataFrame) -> pd.Series:
    src = _col(bars, ind.source)
    return src.ewm(span=ind.period, adjust=False, min_periods=ind.period).mean()


def _rsi(ind: RsiIndicator, bars: pd.DataFrame) -> pd.Series:
    return rsi(_col(bars, "close"), ind.period)


def _roc(ind: RocIndicator, bars: pd.DataFrame) -> pd.Series:
    return trailing_return(_col(bars, "close"), ind.period)


def _zscore(ind: ZScoreIndicator, bars: pd.DataFrame) -> pd.Series:
    z = rolling_zscore(_col(bars, ind.source), ind.period)
    return pd.to_numeric(z, errors="coerce").astype(float)


def _atr(ind: AtrIndicator, bars: pd.DataFrame) -> pd.Series:
    high, low, close = _col(bars, "high"), _col(bars, "low"), _col(bars, "close")
    prev = close.shift(1)
    true_range = pd.concat([high - low, (high - prev).abs(), (low - prev).abs()], axis=1).max(
        axis=1
    )
    return true_range.rolling(ind.period, min_periods=ind.period).mean()


def _donchian_high(ind: DonchianHighIndicator, bars: pd.DataFrame) -> pd.Series:
    # shift(1): the channel is built from the bars before the current one,
    # so a close can break out of it.
    return _col(bars, "high").rolling(ind.period, min_periods=ind.period).max().shift(1)


def _donchian_low(ind: DonchianLowIndicator, bars: pd.DataFrame) -> pd.Series:
    return _col(bars, "low").rolling(ind.period, min_periods=ind.period).min().shift(1)


def _efficiency_ratio(ind: EfficiencyRatioIndicator, bars: pd.DataFrame) -> pd.Series:
    return efficiency_ratio(_col(bars, "close"), ind.period)


def _kama(ind: KamaIndicator, bars: pd.DataFrame) -> pd.Series:
    return kama(_col(bars, "close"), ind.period, ind.fast, ind.slow)


_COMPUTE: dict[type, Callable[..., pd.Series]] = {
    CloseIndicator: _close,
    VolumeIndicator: _volume,
    SmaIndicator: _sma,
    EmaIndicator: _ema,
    RsiIndicator: _rsi,
    RocIndicator: _roc,
    ZScoreIndicator: _zscore,
    AtrIndicator: _atr,
    DonchianHighIndicator: _donchian_high,
    DonchianLowIndicator: _donchian_low,
    EfficiencyRatioIndicator: _efficiency_ratio,
    KamaIndicator: _kama,
}


def compute_indicator(ind: object, bars: pd.DataFrame) -> pd.Series:
    """The indicator's series over ``bars`` (NaN while warming up)."""
    if bars.empty:
        return pd.Series(dtype=float)
    return _COMPUTE[type(ind)](ind, bars).astype(float)


def required_bars(ind: object) -> int:
    """Bars needed for the indicator's value on the last bar to be warm."""
    if isinstance(ind, CloseIndicator | VolumeIndicator):
        return 1
    period = int(ind.period)
    if isinstance(ind, EmaIndicator):
        return RECURSIVE_WARMUP * period
    if isinstance(ind, KamaIndicator):
        return ind.period + RECURSIVE_WARMUP * ind.slow
    if isinstance(ind, RsiIndicator):
        return RECURSIVE_WARMUP * period + 1
    if isinstance(
        ind,
        RocIndicator
        | AtrIndicator
        | DonchianHighIndicator
        | DonchianLowIndicator
        | EfficiencyRatioIndicator,
    ):
        return period + 1
    return period

"""Rule indicators + condition interpreter."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from stonks.features.library import rolling_zscore, rsi, trailing_return
from stonks.strategies.rules.indicators import compute_indicator, required_bars
from stonks.strategies.rules.interpreter import evaluate, snapshot, window_size
from stonks.strategies.rules.spec import validate_spec


def _bars(closes, *, spread=1.0) -> pd.DataFrame:
    closes = np.asarray(closes, dtype=float)
    return pd.DataFrame(
        {
            "timestamp": pd.bdate_range("2026-01-01", periods=len(closes)),
            "open": closes,
            "high": closes + spread,
            "low": closes - spread,
            "close": closes,
            "volume": np.arange(len(closes), dtype=float) + 1,
        }
    )


def _spec(indicators, entry, exit_=None, rank_by=None) -> object:
    return validate_spec(
        {
            "version": 1,
            "indicators": indicators,
            "entry": entry,
            "exit": exit_,
            "rank": {"by": rank_by or indicators[0]["id"]},
        }
    )


def _ind(**kw):
    return _spec([{"id": "x", **kw}], _cmp("x", ">", 0)).indicators[0]


def _cmp(left, op, right):
    def operand(v):
        if isinstance(v, str):
            return {"type": "indicator", "id": v}
        return {"type": "constant", "value": v}

    return {"type": "compare", "left": operand(left), "op": op, "right": operand(right)}


# ---- indicators -------------------------------------------------------------

CLOSES = [10, 11, 12, 11, 13, 14, 13, 15, 16, 15, 17, 18, 17, 19, 20, 19, 21, 22, 21, 23]


def test_close_and_volume():
    bars = _bars(CLOSES)
    assert compute_indicator(_ind(kind="close"), bars).iloc[-1] == 23
    assert compute_indicator(_ind(kind="volume"), bars).iloc[-1] == 20


def test_sma_and_ema():
    bars = _bars(CLOSES)
    sma = compute_indicator(_ind(kind="sma", period=5), bars)
    assert sma.iloc[-1] == pytest.approx(np.mean(CLOSES[-5:]))
    assert math.isnan(sma.iloc[3])
    ema = compute_indicator(_ind(kind="ema", period=5), bars)
    expected = pd.Series(CLOSES, dtype=float).ewm(span=5, adjust=False, min_periods=5).mean()
    assert ema.iloc[-1] == pytest.approx(expected.iloc[-1])
    vol_sma = compute_indicator(_ind(kind="sma", period=2, source="volume"), bars)
    assert vol_sma.iloc[-1] == pytest.approx(19.5)


def test_library_backed_indicators():
    bars = _bars(CLOSES)
    s = pd.Series(CLOSES, dtype=float)
    assert compute_indicator(_ind(kind="rsi", period=5), bars).iloc[-1] == pytest.approx(
        rsi(s, 5).iloc[-1]
    )
    for kind in ("roc", "trailing_return"):
        assert compute_indicator(_ind(kind=kind, period=3), bars).iloc[-1] == pytest.approx(
            trailing_return(s, 3).iloc[-1]
        )
    assert compute_indicator(_ind(kind="zscore", period=5), bars).iloc[-1] == pytest.approx(
        float(rolling_zscore(s, 5).iloc[-1])
    )


def test_zscore_of_flat_series_is_nan_not_error():
    z = compute_indicator(_ind(kind="zscore", period=3), _bars([5.0] * 6))
    assert z.dtype == float
    assert math.isnan(z.iloc[-1])


def test_atr():
    bars = _bars([10, 12, 11], spread=1.0)
    # true ranges: 2 (h-l), max(2, |13-10|, |11-10|)=3, max(2, |12-12|, |10-12|)=2
    atr = compute_indicator(_ind(kind="atr", period=2), bars)
    assert atr.iloc[-1] == pytest.approx(2.5)


def test_donchian_excludes_current_bar():
    bars = _bars([10, 11, 12, 20], spread=0.0)
    high = compute_indicator(_ind(kind="donchian_high", period=3), bars)
    low = compute_indicator(_ind(kind="donchian_low", period=3), bars)
    assert high.iloc[-1] == 12  # prior three highs, not today's 20
    assert low.iloc[-1] == 10
    assert math.isnan(high.iloc[2])


def test_required_bars():
    assert required_bars(_ind(kind="close")) == 1
    assert required_bars(_ind(kind="sma", period=20)) == 20
    assert required_bars(_ind(kind="roc", period=20)) == 21
    assert required_bars(_ind(kind="donchian_high", period=20)) == 21
    assert required_bars(_ind(kind="ema", period=10)) >= 30
    spec = _spec(
        [{"id": "a", "kind": "sma", "period": 20}, {"id": "b", "kind": "roc", "period": 5}],
        _cmp("a", ">", "b"),
    )
    # one extra bar so crosses_* can read the previous value
    assert window_size(spec) == 21


# ---- interpreter ------------------------------------------------------------


def _vals(**pairs):
    return {k: (float(v[0]), float(v[1])) for k, v in pairs.items()}


@pytest.mark.parametrize(
    ("op", "cur", "expected"),
    [("<", 1, True), ("<", 2, False), ("<=", 2, True), (">", 3, True), (">=", 2, True)],
)
def test_comparisons_use_current_bar(op, cur, expected):
    cond = _spec([{"id": "a", "kind": "close"}], _cmp("a", op, 2)).entry
    assert evaluate(cond, _vals(a=(100, cur))) is expected


def test_nan_comparisons_are_false():
    cond = _spec([{"id": "a", "kind": "close"}], _cmp("a", ">", 2)).entry
    assert evaluate(cond, _vals(a=(1, float("nan")))) is False


def test_all_any_not():
    spec = _spec(
        [{"id": "a", "kind": "close"}, {"id": "b", "kind": "volume"}],
        {
            "type": "all",
            "conditions": [
                _cmp("a", ">", 1),
                {"type": "any", "conditions": [_cmp("b", "<", 0), _cmp("b", ">", 5)]},
                {"type": "not", "condition": _cmp("a", ">", 10)},
            ],
        },
    )
    assert evaluate(spec.entry, _vals(a=(0, 2), b=(0, 6))) is True
    assert evaluate(spec.entry, _vals(a=(0, 2), b=(0, 3))) is False
    assert evaluate(spec.entry, _vals(a=(0, 11), b=(0, 6))) is False


def test_crosses_above_fires_only_on_the_crossing_bar():
    cond = _spec([{"id": "a", "kind": "close"}], _cmp("a", "crosses_above", 10)).entry
    assert evaluate(cond, _vals(a=(9, 11))) is True  # the crossing bar
    assert evaluate(cond, _vals(a=(10, 11))) is True  # from touching to above
    assert evaluate(cond, _vals(a=(11, 12))) is False  # the bar after
    assert evaluate(cond, _vals(a=(8, 9))) is False  # still below
    assert evaluate(cond, _vals(a=(9, 10))) is False  # touching is not above
    assert evaluate(cond, _vals(a=(float("nan"), 11))) is False


def test_crosses_below_mirrors():
    cond = _spec([{"id": "a", "kind": "close"}], _cmp("a", "crosses_below", 10)).entry
    assert evaluate(cond, _vals(a=(11, 9))) is True
    assert evaluate(cond, _vals(a=(10, 9))) is True
    assert evaluate(cond, _vals(a=(9, 8))) is False
    assert evaluate(cond, _vals(a=(11, 10))) is False


def test_crosses_between_two_indicators():
    cond = _spec(
        [{"id": "f", "kind": "close"}, {"id": "s", "kind": "volume"}],
        _cmp("f", "crosses_above", "s"),
    ).entry
    assert evaluate(cond, _vals(f=(1, 3), s=(2, 2.5))) is True
    assert evaluate(cond, _vals(f=(3, 4), s=(2, 2.5))) is False


def test_snapshot_crossing_on_real_series():
    # close crosses above its prior 3-bar high exactly on the last bar
    spec = _spec(
        [{"id": "c", "kind": "close"}, {"id": "h", "kind": "donchian_high", "period": 3}],
        _cmp("c", "crosses_above", "h"),
    )
    bars = _bars([10, 10, 10, 10, 10, 12], spread=0.0)
    values = snapshot(spec, bars)
    assert values is not None
    assert evaluate(spec.entry, values) is True
    after = _bars([10, 10, 10, 10, 10, 12, 13], spread=0.0)
    assert evaluate(spec.entry, snapshot(spec, after)) is False


def test_snapshot_none_when_warming_up():
    spec = _spec([{"id": "s", "kind": "sma", "period": 5}], _cmp("s", ">", 0))
    assert snapshot(spec, _bars([1, 2, 3])) is None
    assert snapshot(spec, _bars([])) is None
    assert snapshot(spec, _bars([1, 2, 3, 4, 5])) is not None

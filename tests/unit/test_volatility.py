"""Volatility estimators (BL-09): hand-computed values, GBM recovery,
EWMA equivalences and causality."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from stonks.core.interval import Interval
from stonks.features import volatility as vol

SIGMA = 0.02  # true per-bar sigma of the simulated GBM


def _gbm_ohlc(
    n_bars: int = 1500,
    steps: int = 390,
    sigma: float = SIGMA,
    gap_sigma: float = 0.0,
    seed: int = 7,
) -> pd.DataFrame:
    """Seeded driftless GBM sampled ``steps`` times per bar. ``gap_sigma``
    adds an overnight jump between one bar's close and the next open."""
    rng = np.random.default_rng(seed)
    step_sigma = sigma / math.sqrt(steps)
    increments = rng.normal(-0.5 * step_sigma**2, step_sigma, size=(n_bars, steps))
    gaps = rng.normal(0.0, gap_sigma, size=n_bars) if gap_sigma else np.zeros(n_bars)
    rows = []
    log_price = math.log(100.0)
    for i in range(n_bars):
        log_open = log_price + gaps[i]
        path = log_open + np.cumsum(increments[i])
        full = np.concatenate([[log_open], path])
        rows.append(
            (
                math.exp(log_open),
                math.exp(full.max()),
                math.exp(full.min()),
                math.exp(path[-1]),
            )
        )
        log_price = path[-1]
    idx = pd.date_range("2020-01-01", periods=n_bars, freq="D")
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx)


# --- hand-computed values ---------------------------------------------------


def test_close_to_close_matches_hand_value() -> None:
    close = pd.Series([100.0, 110.0, 99.0, 108.9])
    r = np.log([1.1, 0.9, 1.1])
    expected = float(np.std(r[-2:], ddof=1))
    out = vol.close_to_close(close, n=2)
    assert math.isnan(out.iloc[0]) and math.isnan(out.iloc[1])
    assert out.iloc[3] == pytest.approx(expected)
    assert out.iloc[2] == pytest.approx(float(np.std(r[:2], ddof=1)))


def test_parkinson_matches_hand_value() -> None:
    high = pd.Series([105.0, 110.0])
    low = pd.Series([95.0, 100.0])
    hl = np.log([105 / 95, 110 / 100])
    expected = math.sqrt(np.mean(hl**2) / (4 * math.log(2)))
    assert vol.parkinson(high, low, n=2).iloc[1] == pytest.approx(expected)


def test_garman_klass_matches_hand_value() -> None:
    o, h, lo, c = 100.0, 106.0, 97.0, 103.0
    expected = math.sqrt(0.5 * math.log(h / lo) ** 2 - (2 * math.log(2) - 1) * math.log(c / o) ** 2)
    out = vol.garman_klass(pd.Series([o]), pd.Series([h]), pd.Series([lo]), pd.Series([c]), n=1)
    assert out.iloc[0] == pytest.approx(expected)


def test_rogers_satchell_matches_hand_value() -> None:
    o, h, lo, c = 100.0, 106.0, 97.0, 103.0
    expected = math.sqrt(math.log(h / c) * math.log(h / o) + math.log(lo / c) * math.log(lo / o))
    out = vol.rogers_satchell(pd.Series([o]), pd.Series([h]), pd.Series([lo]), pd.Series([c]), n=1)
    assert out.iloc[0] == pytest.approx(expected)


def test_yang_zhang_matches_hand_value() -> None:
    o = pd.Series([100.0, 101.0, 99.0, 104.0])
    h = pd.Series([102.0, 103.0, 101.0, 106.0])
    lo = pd.Series([99.0, 100.0, 97.0, 102.0])
    c = pd.Series([101.0, 100.0, 100.0, 105.0])
    n = 3
    overnight = np.log(o.values[1:] / c.values[:-1])
    open_close = np.log(c.values[1:] / o.values[1:])
    rs = np.log(h / c) * np.log(h / o) + np.log(lo / c) * np.log(lo / o)
    k = 0.34 / (1.34 + (n + 1) / (n - 1))
    var = (
        np.var(overnight, ddof=1)
        + k * np.var(open_close, ddof=1)
        + (1 - k) * float(np.mean(rs.values[1:]))
    )
    out = vol.yang_zhang(o, h, lo, c, n=n)
    assert out.iloc[3] == pytest.approx(math.sqrt(var))
    # the first window needs n overnight returns, so needs bar 0's close
    assert out.iloc[:3].isna().all()


def test_ewma_vol_matches_recursion() -> None:
    r = pd.Series([0.01, -0.02, 0.03])
    lam = 1 - 2 / (3 + 1)  # span 3 -> alpha 0.5
    var = r.iloc[0] ** 2
    for x in r.iloc[1:]:
        var = lam * var + (1 - lam) * x**2
    assert vol.ewma_vol(r, span=3, min_periods=1).iloc[-1] == pytest.approx(math.sqrt(var))


def test_ewma_span_and_riskmetrics_lambda_are_equivalent() -> None:
    r = pd.Series(np.random.default_rng(1).normal(0, 0.01, 300))
    span = 35
    lam = 1 - 2 / (span + 1)
    pd.testing.assert_series_equal(
        vol.ewma_vol(r, span=span), vol.riskmetrics_vol(r, lam=lam, min_periods=span)
    )


def test_riskmetrics_default_lambda_is_094() -> None:
    r = pd.Series(np.random.default_rng(2).normal(0, 0.01, 100))
    pd.testing.assert_series_equal(vol.riskmetrics_vol(r), vol.riskmetrics_vol(r, lam=0.94))


def test_floor_vol_lifts_to_trailing_percentile() -> None:
    sigma = pd.Series([1.0, 2.0, 3.0, 4.0, 0.5])
    out = vol.floor_vol(sigma, pct=0.5, window=5)
    # trailing median at the last bar is 2.0 > 0.5
    assert out.iloc[-1] == pytest.approx(2.0)
    assert out.iloc[3] == pytest.approx(4.0)
    assert (out >= sigma).all()


def test_annualize_scales_by_sqrt_periods() -> None:
    assert vol.annualize(pd.Series([0.01])).iloc[0] == pytest.approx(0.01 * math.sqrt(252))
    assert vol.annualize(0.01, periods_per_year=365) == pytest.approx(0.01 * math.sqrt(365))


def test_periods_per_year_reuses_the_trading_calendar() -> None:
    assert vol.periods_per_year("equity", Interval.parse("1d")) == 252
    assert vol.periods_per_year("crypto", Interval.parse("1d")) == 365


def test_bad_window_raises() -> None:
    with pytest.raises(ValueError):
        vol.close_to_close(pd.Series([1.0, 2.0]), n=1)
    with pytest.raises(ValueError):
        vol.yang_zhang(*(pd.Series([1.0, 1.0]),) * 4, n=1)
    with pytest.raises(ValueError):
        vol.riskmetrics_vol(pd.Series([0.1]), lam=1.0)


# --- GBM recovery -----------------------------------------------------------


@pytest.fixture(scope="module")
def gbm() -> pd.DataFrame:
    return _gbm_ohlc()


N = 1000


@pytest.mark.parametrize(
    "estimator",
    ["close_to_close", "parkinson", "garman_klass", "rogers_satchell", "yang_zhang", "ewma"],
)
def test_every_estimator_recovers_gbm_sigma_within_10pct(gbm: pd.DataFrame, estimator: str) -> None:
    o, h, lo, c = gbm["open"], gbm["high"], gbm["low"], gbm["close"]
    if estimator == "close_to_close":
        out = vol.close_to_close(c, n=N)
    elif estimator == "parkinson":
        out = vol.parkinson(h, lo, n=N)
    elif estimator == "garman_klass":
        out = vol.garman_klass(o, h, lo, c, n=N)
    elif estimator == "rogers_satchell":
        out = vol.rogers_satchell(o, h, lo, c, n=N)
    elif estimator == "yang_zhang":
        out = vol.yang_zhang(o, h, lo, c, n=N)
    else:
        out = vol.ewma_vol(np.log(c).diff(), span=N)
    assert out.iloc[-1] == pytest.approx(SIGMA, rel=0.10)


def test_yang_zhang_equals_rogers_satchell_without_gaps(gbm: pd.DataFrame) -> None:
    """No overnight gaps: sigma_o is 0 and, in expectation, the open-to-close
    variance equals the RS variance, so YZ collapses onto RS."""
    o, h, lo, c = gbm["open"], gbm["high"], gbm["low"], gbm["close"]
    yz = vol.yang_zhang(o, h, lo, c, n=N).iloc[-1]
    rs = vol.rogers_satchell(o, h, lo, c, n=N).iloc[-1]
    assert yz == pytest.approx(rs, rel=0.05)


def test_yang_zhang_captures_overnight_gaps_that_rs_misses() -> None:
    gap = 0.015
    df = _gbm_ohlc(gap_sigma=gap, seed=11)
    o, h, lo, c = df["open"], df["high"], df["low"], df["close"]
    yz = vol.yang_zhang(o, h, lo, c, n=N).iloc[-1]
    rs = vol.rogers_satchell(o, h, lo, c, n=N).iloc[-1]
    assert yz == pytest.approx(math.sqrt(SIGMA**2 + gap**2), rel=0.10)
    assert rs == pytest.approx(SIGMA, rel=0.10)


# --- causality --------------------------------------------------------------


@pytest.mark.parametrize(
    "fn",
    [
        lambda d: vol.close_to_close(d["close"], n=20),
        lambda d: vol.parkinson(d["high"], d["low"], n=20),
        lambda d: vol.garman_klass(d["open"], d["high"], d["low"], d["close"], n=20),
        lambda d: vol.rogers_satchell(d["open"], d["high"], d["low"], d["close"], n=20),
        lambda d: vol.yang_zhang(d["open"], d["high"], d["low"], d["close"], n=20),
        lambda d: vol.ewma_vol(np.log(d["close"]).diff(), span=20),
        lambda d: vol.floor_vol(vol.close_to_close(d["close"], n=5), window=50),
    ],
)
def test_estimators_are_causal(fn) -> None:
    """Planting an extreme future bar never changes any earlier value."""
    df = _gbm_ohlc(n_bars=120, steps=20)
    base = fn(df)
    shocked = df.copy()
    shocked.iloc[100:] = shocked.iloc[100:] * np.array([3.0, 4.0, 0.5, 2.0])
    after = fn(shocked)
    pd.testing.assert_series_equal(base.iloc[:100], after.iloc[:100])


def test_works_column_wise_on_frames(gbm: pd.DataFrame) -> None:
    closes = pd.DataFrame({"A": gbm["close"], "B": gbm["close"] * 2})
    out = vol.close_to_close(closes, n=50)
    pd.testing.assert_series_equal(out["A"], out["B"], check_names=False)

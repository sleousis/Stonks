"""Signal IC analysis (BL-33)."""

from __future__ import annotations

import json
import math
from datetime import timedelta

import numpy as np
import pandas as pd
import pytest

from stonks.lab.signal_eval import (
    SignalICResult,
    forward_returns,
    ic_by_date,
    score_panel,
    signal_ic,
)
from tests.fixtures.signal_research import (
    FutureReturnSignal,
    NextGapSignal,
    NoiseSignal,
    StaticSignal,
    dataset_for,
    signal_lake,
)

TICKERS = [f"T{i:02d}" for i in range(12)]


@pytest.fixture(scope="module")
def lake():
    lake = signal_lake(":memory:", TICKERS, periods=220, seed=3)
    yield lake
    lake.close()


def _ds(lake, universe=TICKERS):
    return dataset_for(lake, universe)


def _horizon(result: SignalICResult, h: int):
    return next(x for x in result.horizons if x.horizon == h)


def test_forward_returns_start_at_the_next_open(lake):
    ds = _ds(lake)
    bars = lake.sql(
        "SELECT timestamp, open FROM bars WHERE ticker = 'T00' AND interval = '1d' ORDER BY 1"
    )
    ts = pd.DatetimeIndex(pd.to_datetime(bars["timestamp"]))
    opens = bars["open"].to_numpy()
    fwd = forward_returns(ds, ds.full_window, (1, 5), ts[:10])
    assert fwd[1].loc[ts[0], "T00"] == pytest.approx(opens[2] / opens[1] - 1)
    assert fwd[5].loc[ts[3], "T00"] == pytest.approx(opens[9] / opens[4] - 1)


def test_forward_returns_never_read_past_the_window_end(lake):
    ds = _ds(lake)
    ts = pd.DatetimeIndex(
        pd.to_datetime(lake.sql("SELECT DISTINCT timestamp FROM bars ORDER BY 1")["timestamp"])
    )
    window = (ds.start, ts[49].date())
    fwd = forward_returns(ds, window, (1, 5), ts[:50])
    # t=44 needs open[50] (outside the window) for h=5; t=43 needs open[49]
    assert np.isnan(fwd[5].loc[ts[44], "T00"])
    assert np.isfinite(fwd[5].loc[ts[43], "T00"])
    assert np.isnan(fwd[1].loc[ts[48], "T00"])
    assert np.isfinite(fwd[1].loc[ts[47], "T00"])


def test_planted_signal_has_ic_one(lake):
    result = signal_ic(
        FutureReturnSignal({"horizon": 1}), _ds(lake), horizons=(1, 5), every_bars=1, max_workers=1
    )
    assert result.status == "ok"
    h1 = _horizon(result, 1)
    assert h1.mean_ic == pytest.approx(1.0)
    assert h1.t_stat_hac > 10
    assert h1.spread_mean > 0
    assert h1.quantile_means[-1] > h1.quantile_means[0]
    # decays: the one-bar return says little about the five-bar one
    assert _horizon(result, 5).mean_ic < 0.8


def test_signal_that_peeks_at_the_next_gap_scores_nothing(lake):
    """Returns start at the next open, so the gap into t+1 is not part of
    them: a score built from it has no IC. A misaligned evaluation (returns
    from the close at t) would credit it."""
    result = signal_ic(NextGapSignal({}), _ds(lake), horizons=(1,), every_bars=1, max_workers=1)
    assert abs(_horizon(result, 1).mean_ic) < 0.1


def test_noise_signal_has_no_ic(lake):
    result = signal_ic(
        NoiseSignal({"seed": 5}), _ds(lake), horizons=(1, 5, 21), every_bars=1, max_workers=1
    )
    for h in result.horizons:
        assert abs(h.mean_ic) < 0.05
        assert abs(h.t_stat_hac) < 2


def test_hac_se_exceeds_iid_se_on_overlapping_horizons(lake):
    result = signal_ic(StaticSignal({}), _ds(lake), horizons=(21,), every_bars=1, max_workers=1)
    h = _horizon(result, 21)
    assert h.hac_lags == 20
    assert h.se_hac > 1.5 * h.se_iid


def test_hac_lags_scale_with_sampling(lake):
    result = signal_ic(StaticSignal({}), _ds(lake), horizons=(1, 21), every_bars=5, max_workers=1)
    assert _horizon(result, 1).hac_lags == 0
    assert _horizon(result, 21).hac_lags == 4  # ceil(21 / 5) - 1


def test_static_signal_has_zero_turnover(lake):
    result = signal_ic(StaticSignal({}), _ds(lake), horizons=(1,), every_bars=5, max_workers=1)
    assert result.score_turnover == pytest.approx(0.0, abs=1e-12)
    assert result.top_quantile_turnover == pytest.approx(0.0, abs=1e-12)
    noisy = signal_ic(NoiseSignal({}), _ds(lake), horizons=(1,), every_bars=5, max_workers=1)
    assert noisy.score_turnover > 0.5


def test_small_universe_is_not_applicable(lake):
    result = signal_ic(StaticSignal({}), _ds(lake, TICKERS[:9]), horizons=(1,), max_workers=1)
    assert result.status == "n/a"
    assert result.horizons == []
    assert "10" in result.note


def test_ic_estimate_uses_the_strategy_horizon(lake):
    class Labelled(FutureReturnSignal):
        label_horizon_bars = 5

    result = signal_ic(Labelled({"horizon": 5}), _ds(lake), horizons=(1, 5), max_workers=1)
    assert result.ic_horizon == 5
    assert result.ic_estimate == pytest.approx(_horizon(result, 5).mean_ic)
    default = signal_ic(StaticSignal({}), _ds(lake), horizons=(1, 5, 21), max_workers=1)
    assert default.ic_horizon == 21


def test_sampling_every_bars(lake):
    ds = _ds(lake)
    panel = score_panel(StaticSignal({}), ds, ds.full_window, every_bars=5, max_workers=1)
    assert len(panel) == math.ceil(220 / 5)
    gaps = pd.Series(panel.index).diff().dropna()
    assert (gaps >= timedelta(days=5)).all()


def test_ic_by_date_is_spearman():
    idx = pd.DatetimeIndex(["2024-01-02", "2024-01-03"])
    scores = pd.DataFrame([[1.0, 2.0, 3.0, 4.0, 5.0], [5, 4, 3, 2, 1.0]], index=idx)
    fwd = pd.DataFrame([[0.1, 0.2, 0.3, 0.9, 5.0], [0.1, 0.2, 0.3, 0.4, 0.5]], index=idx)
    ic = ic_by_date(scores, fwd, min_names=5)
    assert ic.tolist() == pytest.approx([1.0, -1.0])
    assert ic_by_date(scores.iloc[:, :4], fwd.iloc[:, :4], min_names=5).isna().all()


def test_result_is_plain_data(lake):
    result = signal_ic(StaticSignal({}), _ds(lake), horizons=(1, 5), max_workers=1)
    data = result.to_dict()
    json.dumps(data)
    assert data["strategy_id"] == "static_signal_fake"
    assert [h["horizon"] for h in data["horizons"]] == [1, 5]


def test_rejects_bad_arguments(lake):
    with pytest.raises(ValueError):
        signal_ic(StaticSignal({}), _ds(lake), horizons=(), max_workers=1)
    with pytest.raises(ValueError):
        signal_ic(StaticSignal({}), _ds(lake), horizons=(0,), max_workers=1)
    with pytest.raises(ValueError):
        signal_ic(StaticSignal({}), _ds(lake), every_bars=0, max_workers=1)

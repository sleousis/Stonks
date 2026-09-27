"""LatentRegimeFilter (BL-46): a Markov-switching volatility gate."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from stonks.core.protocols import Strategy
from stonks.core.types import Portfolio
from stonks.lab.catalog import is_wrapper, strategy_catalog
from stonks.lab.dataset import LabDataset
from stonks.strategies.latent_regime import LatentRegimeFilter
from tests.unit.nt888_helpers import make_lake, write_bars

INNER = "tests.unit.test_regime_filter:TwoWayInner"
DATES = pd.bdate_range("2020-01-01", periods=900)
# calm, turbulent, calm, turbulent: the train window holds both states
TURBULENT = [(300, 400), (700, 800)]


def _closes(seed=4):
    rng = np.random.default_rng(seed)
    sigma = np.full(len(DATES), 0.006)
    for lo, hi in TURBULENT:
        sigma[lo:hi] = 0.03
    return 100 * np.exp(np.cumsum(rng.normal(0.0003, sigma)))


@pytest.fixture
def lake(tmp_path):
    db = make_lake(tmp_path / "lake.duckdb")
    write_bars(db, "SPY.US", DATES, _closes())
    yield db
    db.close()


def _dataset(lake):
    return LabDataset(
        lake=lake,
        universe=["NEW.US"],
        start=DATES[0].date(),
        end=DATES[-1].date(),
        train_end=DATES[599].date(),
    )


def _filter(**overrides) -> LatentRegimeFilter:
    return LatentRegimeFilter(
        {"inner_class_path": INNER, "inner_params": {}, "filter_bars": 60, **overrides}
    )


def _day(i):
    return DATES[i].to_pydatetime()


def test_surface_and_catalog():
    f = _filter()
    assert isinstance(f, Strategy)
    assert f.id == "two_way_latent_regime"
    assert f.hypothesis
    assert "SPY.US" in f.data_tickers()
    assert strategy_catalog()["latent_regime_filter"] is LatentRegimeFilter
    assert is_wrapper(LatentRegimeFilter)


def test_fitted_filter_flags_the_turbulent_state_out_of_sample(lake):
    f = _filter()
    f.fit(_dataset(lake))
    assert f.model is not None
    assert f.fitted_state()["sigmas"][1] > 3 * f.fitted_state()["sigmas"][0]
    # validation window: calm before bar 700, turbulent inside
    assert not f.is_risk_off(_day(690), lake)
    assert f.is_risk_off(_day(760), lake)
    assert f.high_vol_probability(_day(760), lake) == pytest.approx(
        f.high_vol_probability(_day(760), lake)
    )


def test_block_mode_drops_buys_and_keeps_sells(lake):
    f = _filter()
    f.fit(_dataset(lake))
    as_of = _day(760)
    assert f.estimate_return("NEW", as_of, lake) is not None
    orders = f.decide([(0.1, "NEW")], Portfolio(cash=1e6, positions={"OLD": 5.0}), {}, as_of)
    assert [o.side for o in orders] == ["sell"]
    calm = f.decide([(0.1, "NEW")], Portfolio(cash=1e6, positions={"OLD": 5.0}), {}, _day(690))
    assert sorted(o.side for o in calm) == ["buy", "sell"]


def test_exit_all_mode_sells_every_long(lake):
    f = _filter(mode="exit_all")
    f.fit(_dataset(lake))
    as_of = _day(760)
    assert f.estimate_return("NEW", as_of, lake) is None
    orders = f.decide([], Portfolio(cash=0.0, positions={"OLD": 5.0, "X": 2.0}), {}, as_of)
    assert sorted(o.ticker for o in orders) == ["OLD", "X"]
    assert all(o.side == "sell" for o in orders)


def test_fit_reads_only_the_train_window(lake, tmp_path):
    closes = _closes()
    closes[600:] = closes[599] * np.exp(np.cumsum(np.random.default_rng(9).normal(0, 0.1, 300)))
    other = make_lake(tmp_path / "other.duckdb")
    try:
        write_bars(other, "SPY.US", DATES, closes)
        a, b = _filter(), _filter()
        a.fit(_dataset(lake))
        b.fit(_dataset(other))
        assert a.fitted_state() == b.fitted_state()
    finally:
        other.close()


def test_the_filter_reads_no_future_bars(lake, tmp_path):
    f = _filter()
    f.fit(_dataset(lake))
    closes = _closes()
    closes[651:] *= 0.5  # a crash after the decision day
    other = make_lake(tmp_path / "other.duckdb")
    try:
        write_bars(other, "SPY.US", DATES, closes)
        assert f.high_vol_probability(_day(650), lake) == pytest.approx(
            f.high_vol_probability(_day(650), other)
        )
    finally:
        other.close()


def test_unfitted_filter_fits_lazily_on_data_up_to_as_of(lake, tmp_path):
    f = _filter(fit_bars=500)
    assert f.high_vol_probability(_day(760), lake) is not None
    assert f.model is not None
    closes = _closes()
    closes[761:] *= np.exp(np.cumsum(np.random.default_rng(2).normal(0, 0.2, len(closes) - 761)))
    other = make_lake(tmp_path / "other.duckdb")
    try:
        write_bars(other, "SPY.US", DATES, closes)
        g = _filter(fit_bars=500)
        g.high_vol_probability(_day(760), other)
        assert g.fitted_state() == f.fitted_state()
    finally:
        other.close()


def test_too_little_history_is_risk_on(lake):
    f = _filter()
    assert f.high_vol_probability(_day(10), lake) is None
    assert not f.is_risk_off(_day(10), lake)
    assert f.model is None
    empty = _filter(regime_ticker="NONE.US")
    empty.fit(_dataset(lake))
    assert empty.model is None and empty.fitted_state() == {}


def test_features_expose_the_probability(lake):
    f = _filter()
    f.fit(_dataset(lake))
    values = f.extract_features("NEW", _day(760), lake).values
    assert values["regime_risk_off"] == 1.0
    assert values["regime_p_high_vol"] > 0.7
    assert "regime_p_high_vol" not in f.extract_features("NEW", _day(10), lake).values
    assert f.extract_features("NEW", _day(10), None).values == {}
    assert f.estimate_return("NEW", _day(10), None) is not None


def test_save_load_round_trip(lake, tmp_path):
    f = _filter()
    f.fit(_dataset(lake))
    f.save(tmp_path / "art")
    loaded = LatentRegimeFilter.load(tmp_path / "art")
    assert loaded.fitted_state() == f.fitted_state()
    assert loaded.high_vol_probability(_day(760), lake) == pytest.approx(
        f.high_vol_probability(_day(760), lake)
    )
    unfitted = _filter()
    unfitted.save(tmp_path / "u")
    assert LatentRegimeFilter.load(tmp_path / "u").model is None

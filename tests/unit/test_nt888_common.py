"""Contract tests shared by every neurotrader888 indicator strategy:
parameter surface, asset classes, the long/flat ``decide`` pattern,
persistence, and look-ahead safety (appending future bars never changes a
past answer)."""

from __future__ import annotations

from datetime import date

import pytest

from stonks.core.types import Portfolio
from stonks.strategies.examples.intramarket_difference import IntramarketDifferenceStrategy
from stonks.strategies.examples.ma_crossover import MACrossoverStrategy
from stonks.strategies.examples.market_profile_sr import MarketProfileSRStrategy
from stonks.strategies.examples.visibility_graph_path import VisibilityGraphPathStrategy
from stonks.strategies.examples.volatility_hawkes import VolatilityHawkesStrategy
from stonks.strategies.examples.vsa import VSAStrategy
from tests.nt888_bars import as_of, random_walk, seed_lake

T = "X.CC"

CASES = [
    (VolatilityHawkesStrategy, {"kappa": 0.5, "quantile_lookback": 24, "norm_lookback": 50}),
    (VisibilityGraphPathStrategy, {"lookback": 12}),
    (VSAStrategy, {"norm_lookback": 48, "threshold": 0.5, "hold_bars": 6}),
    (MarketProfileSRStrategy, {"lookback": 100}),
    (IntramarketDifferenceStrategy, {"lookback": 6, "atr_lookback": 24, "reference_ticker": "REF"}),
    (MACrossoverStrategy, {"fast": 3, "slow": 10}),
]
IDS = [c[0].__name__ for c in CASES]


def _make(cls, params, **extra):
    return cls({**params, "ticker": T, **extra})


@pytest.mark.parametrize(("cls", "params"), CASES, ids=IDS)
def test_common_params_are_present_and_not_tunable(cls, params):
    specs = {s.name: s for s in cls.parameter_spec()}
    for name in ("ticker", "interval", "allocation"):
        assert name in specs
        assert specs[name].tunable is False
    assert specs["interval"].default == "1h"
    assert any(s.tunable for s in specs.values())
    # defaults alone must be valid
    cls({})


@pytest.mark.parametrize(("cls", "params"), CASES, ids=IDS)
def test_asset_classes(cls, params):
    assert cls.applicable_asset_classes == ("crypto", "equity")


@pytest.mark.parametrize(("cls", "params"), CASES, ids=IDS)
def test_ids_are_unique_and_meaningful(cls, params):
    assert cls.id not in ("base", "")
    assert len({c.id for c, _ in CASES}) == len(CASES)


@pytest.fixture(scope="module")
def walk_lake(tmp_path_factory):
    frame = random_walk(420, seed=11)
    ref = random_walk(420, seed=12, start_price=50.0)
    lake = seed_lake(tmp_path_factory.mktemp("walk") / "lake.duckdb", {T: frame, "REF": ref})
    yield lake, frame
    lake.close()


@pytest.mark.parametrize(("cls", "params"), CASES, ids=IDS)
def test_estimate_return_none_for_other_ticker_or_no_lake(cls, params, walk_lake):
    lake, frame = walk_lake
    s = _make(cls, params)
    assert s.estimate_return("OTHER.CC", as_of(frame, -1), lake) is None
    assert s.estimate_return(T, as_of(frame, -1), None) is None


@pytest.mark.parametrize(("cls", "params"), CASES, ids=IDS)
def test_none_before_any_history(cls, params, walk_lake):
    lake, frame = walk_lake
    s = _make(cls, params)
    assert s.estimate_return(T, as_of(frame, 2), lake) is None
    assert s.extract_features(T, as_of(frame, 2), lake).values == {}


@pytest.mark.parametrize(("cls", "params"), CASES, ids=IDS)
def test_features_have_signal_once_warm(cls, params, walk_lake):
    lake, frame = walk_lake
    values = _make(cls, params).extract_features(T, as_of(frame, -1), lake).values
    assert values["signal"] in (0.0, 1.0)


@pytest.mark.parametrize(("cls", "params"), CASES, ids=IDS)
def test_estimate_return_positive_exactly_when_signal_long(cls, params, walk_lake):
    lake, frame = walk_lake
    s = _make(cls, params)
    seen_long = False
    for i in range(300, 420):
        t = as_of(frame, i)
        r = s.estimate_return(T, t, lake)
        sig = s.extract_features(T, t, lake).values.get("signal", 0.0)
        assert (r is not None) == (sig == 1.0), i
        if r is not None:
            assert r > 0
            seen_long = True
    assert seen_long, "the random walk should trigger at least one long bar"


@pytest.mark.parametrize(("cls", "params"), CASES, ids=IDS)
def test_causality_future_bars_do_not_change_past(cls, params, walk_lake, tmp_path):
    lake_full, frame = walk_lake
    cut = 360
    ref = random_walk(420, seed=12, start_price=50.0)
    lake_cut = seed_lake(tmp_path / "cut.duckdb", {T: frame.iloc[:cut], "REF": ref.iloc[:cut]})
    try:
        a, b = _make(cls, params), _make(cls, params)
        for i in range(cut - 30, cut):
            t = as_of(frame, i)
            assert a.estimate_return(T, t, lake_cut) == b.estimate_return(T, t, lake_full), i
            fa = a.extract_features(T, t, lake_cut).values
            fb = b.extract_features(T, t, lake_full).values
            assert fa == pytest.approx(fb, nan_ok=True), i
    finally:
        lake_cut.close()


@pytest.mark.parametrize(("cls", "params"), CASES, ids=IDS)
def test_decide_buys_when_picked_and_flat(cls, params):
    s = _make(cls, params, allocation=0.5)
    orders = s.decide(
        [(0.1, T)], Portfolio(cash=10_000.0, positions={}), {T: 100.0}, date(2026, 2, 1)
    )
    assert len(orders) == 1
    assert orders[0].side == "buy"
    assert orders[0].ticker == T
    assert orders[0].quantity == pytest.approx(50.0)
    assert orders[0].strategy_id == cls.id


@pytest.mark.parametrize(("cls", "params"), CASES, ids=IDS)
def test_decide_sells_when_not_picked_and_holding(cls, params):
    s = _make(cls, params)
    orders = s.decide([], Portfolio(cash=0.0, positions={T: 7.0}), {T: 100.0}, date(2026, 2, 1))
    assert [(o.side, o.quantity) for o in orders] == [("sell", 7.0)]


@pytest.mark.parametrize(("cls", "params"), CASES, ids=IDS)
def test_decide_holds_when_picked_and_holding(cls, params):
    s = _make(cls, params)
    port = Portfolio(cash=500.0, positions={T: 7.0})
    assert s.decide([(0.1, T)], port, {T: 100.0}, date(2026, 2, 1)) == []
    assert s.decide([], Portfolio(cash=500.0, positions={}), {T: 100.0}, date(2026, 2, 1)) == []


@pytest.mark.parametrize(("cls", "params"), CASES, ids=IDS)
def test_save_load_round_trip(cls, params, tmp_path):
    s = _make(cls, params)
    s.save(tmp_path / "a")
    loaded = cls.load(tmp_path / "a")
    assert isinstance(loaded, cls)
    assert loaded.params == s.params


@pytest.mark.parametrize(("cls", "params"), CASES, ids=IDS)
def test_registry_round_trip(cls, params, tmp_path):
    from stonks.registry.store import StrategyRegistry
    from stonks.store.state import SqliteState

    state = SqliteState(tmp_path / "state.sqlite")
    state.migrate()
    try:
        reg = StrategyRegistry(state=state, artifacts_dir=tmp_path / "artifacts")
        s = _make(cls, params)
        sid = reg.register(s, reports=[])
        loaded = reg.load(sid)
        assert type(loaded) is cls
        assert loaded.params == s.params
    finally:
        state.close()

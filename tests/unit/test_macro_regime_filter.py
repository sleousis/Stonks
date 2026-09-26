"""MacroRegimeFilter: gate any strategy on a point-in-time macro regime."""

from __future__ import annotations

from datetime import date, datetime

import pandas as pd
import pytest

from stonks.core.protocols import Strategy
from stonks.core.types import Order, Portfolio
from stonks.store.lake import DuckDBLake
from stonks.strategies.base import BaseStrategy
from stonks.strategies.examples.buy_and_hold import BuyAndHold
from stonks.strategies.macro_regime import MacroRegimeFilter

BUY_AND_HOLD = "stonks.strategies.examples.buy_and_hold:BuyAndHold"
IND = "unemployment_total_percent"


def _macro(lake, points, *, country="USA", indicator=IND):
    lake.upsert_macro_indicators(
        pd.DataFrame(
            [
                {
                    "country_iso": country,
                    "indicator": indicator,
                    "observation_date": d,
                    "period": "annual",
                    "country_name": "x",
                    "value": v,
                }
                for d, v in points
            ]
        )
    )


@pytest.fixture
def lake(tmp_path):
    db = DuckDBLake(tmp_path / "lake.duckdb")
    db.migrate()
    # unemployment: 3.5 -> 3.6 (+0.1) -> 4.6 (+1.0)
    _macro(db, [(date(2021, 12, 31), 3.5), (date(2022, 12, 31), 3.6), (date(2023, 12, 31), 4.6)])
    yield db
    db.close()


def _filter(**overrides):
    params = {
        "inner_class_path": BUY_AND_HOLD,
        "inner_params": {"ticker": "AAPL.US"},
        "country_iso": "USA",
        "indicator": IND,
        "transform": "change",
        "threshold": 0.5,
        "risk_off_when": "above",
        "publication_lag_days": 90,
        **overrides,
    }
    return MacroRegimeFilter(params)


# ---- surface ------------------------------------------------------------------


def test_satisfies_the_strategy_protocol_and_mirrors_the_inner_strategy():
    f = _filter()
    assert isinstance(f, Strategy)
    assert isinstance(f.inner, BuyAndHold)
    assert f.applicable_asset_classes == BuyAndHold.applicable_asset_classes
    assert "buy_and_hold" in f.id


def test_params_carry_the_resolved_inner_params():
    f = _filter()
    assert f.params["inner_class_path"] == BUY_AND_HOLD
    assert f.params["inner_params"] == {"ticker": "AAPL.US", "allocation": 1.0}


@pytest.mark.parametrize(
    "path",
    ["nope.module:Thing", "stonks.strategies.examples.buy_and_hold:Missing", "no_colon", "os:path"],
)
def test_bad_inner_class_path_is_rejected(path):
    with pytest.raises(ValueError, match="inner"):
        _filter(inner_class_path=path)


def test_inner_params_must_be_a_mapping():
    with pytest.raises(ValueError, match="inner_params"):
        _filter(inner_params="ticker=AAPL.US")


# ---- regime -------------------------------------------------------------------


def test_risk_on_passes_through_to_the_inner_strategy(lake):
    f = _filter()
    # 2023-06-01: latest available obs is 2022-12-31 (+0.1) -> risk on
    assert f.estimate_return("AAPL.US", date(2023, 6, 1), lake) == 1.0
    assert f.estimate_return("MSFT.US", date(2023, 6, 1), lake) is None
    assert f.is_risk_off(date(2023, 6, 1), lake) is False


def test_risk_off_blocks_estimates(lake):
    f = _filter()
    assert f.is_risk_off(date(2024, 6, 1), lake) is True
    assert f.estimate_return("AAPL.US", date(2024, 6, 1), lake) is None


def test_publication_lag_hides_fresh_observations(lake):
    f = _filter(publication_lag_days=90)
    # 2023-12-31 + 90 days = 2024-03-30
    assert f.is_risk_off(date(2024, 3, 29), lake) is False
    assert f.is_risk_off(date(2024, 3, 30), lake) is True
    assert f.is_risk_off(datetime(2024, 3, 29, 23, 59), lake) is False
    assert _filter(publication_lag_days=0).is_risk_off(date(2023, 12, 31), lake) is True


def test_level_transform_and_below_direction(lake):
    above = _filter(transform="level", threshold=4.0)
    assert above.is_risk_off(date(2023, 6, 1), lake) is False  # 3.6
    assert above.is_risk_off(date(2024, 6, 1), lake) is True  # 4.6
    below = _filter(transform="level", threshold=4.0, risk_off_when="below")
    assert below.is_risk_off(date(2023, 6, 1), lake) is True
    assert below.is_risk_off(date(2024, 6, 1), lake) is False


def test_change_over_several_observations(lake):
    f = _filter(change_periods=2, threshold=1.0)
    # 4.6 - 3.5 = 1.1 > 1.0
    assert f.is_risk_off(date(2024, 6, 1), lake) is True
    # only two observations visible: not enough history for a 2-step change
    assert f.is_risk_off(date(2023, 6, 1), lake) is False  # unknown -> risk on


def test_unknown_regime_follows_when_unknown(lake):
    on = _filter(indicator="does_not_exist")
    off = _filter(indicator="does_not_exist", when_unknown="risk_off")
    assert on.is_risk_off(date(2024, 6, 1), lake) is False
    assert off.is_risk_off(date(2024, 6, 1), lake) is True
    assert off.estimate_return("AAPL.US", date(2024, 6, 1), lake) is None


def test_stale_observations_count_as_unknown(lake):
    f = _filter(max_staleness_days=400, when_unknown="risk_off", threshold=5.0)
    assert f.is_risk_off(date(2024, 6, 1), lake) is False  # obs 2023-12-31 is fresh
    assert f.is_risk_off(date(2025, 3, 1), lake) is True  # 426 days old -> unknown


def test_null_values_are_skipped(lake):
    _macro(lake, [(date(2024, 12, 31), None)])
    f = _filter(publication_lag_days=0)
    # the NULL 2024 point is ignored; the 2023 change (+1.0) still applies
    assert f.is_risk_off(date(2025, 1, 15), lake) is True


def test_other_countries_do_not_leak_in(lake):
    _macro(lake, [(date(2023, 12, 31), 99.0)], country="DEU")
    f = _filter(transform="level", threshold=10.0)
    assert f.is_risk_off(date(2024, 6, 1), lake) is False


def test_features_expose_the_regime(lake):
    values = _filter().extract_features("AAPL.US", date(2024, 6, 1), lake).values
    assert values["macro_risk_off"] == 1.0
    assert values["macro_signal"] == pytest.approx(1.0)


def test_reads_the_macro_series_once_per_instance(lake):
    class Counting:
        def __init__(self, inner):
            self.inner = inner
            self.calls = 0

        def __getattr__(self, name):
            if name == "get_macro_series":
                self.calls += 1
            return getattr(self.inner, name)

    counting = Counting(lake)
    f = _filter()
    for d in pd.bdate_range("2023-01-02", "2024-12-31", freq="7B"):
        f.estimate_return("AAPL.US", d.date(), counting)
    assert counting.calls == 1


# ---- decide -------------------------------------------------------------------


def test_decide_in_risk_on_delegates(lake):
    f = _filter()
    as_of = date(2023, 6, 1)
    f.estimate_return("AAPL.US", as_of, lake)
    orders = f.decide([(1.0, "AAPL.US")], Portfolio(cash=1000.0), {"AAPL.US": 10.0}, as_of)
    assert [(o.side, o.ticker) for o in orders] == [("buy", "AAPL.US")]


def test_decide_in_risk_off_exits_every_position(lake):
    f = _filter()
    as_of = date(2024, 6, 1)
    f.estimate_return("AAPL.US", as_of, lake)
    portfolio = Portfolio(cash=0.0, positions={"AAPL.US": 5.0, "MSFT.US": 2.0})
    orders = f.decide([], portfolio, {"AAPL.US": 10.0, "MSFT.US": 20.0}, as_of)
    assert sorted((o.side, o.ticker, o.quantity) for o in orders) == [
        ("sell", "AAPL.US", 5.0),
        ("sell", "MSFT.US", 2.0),
    ]
    assert len({o.client_id for o in orders}) == 2


def test_decide_evaluates_the_regime_itself_when_estimate_return_was_not_called(lake):
    f = _filter()
    f.estimate_return("AAPL.US", date(2023, 6, 1), lake)  # remembers the lake
    orders = f.decide([], Portfolio(cash=0.0, positions={"AAPL.US": 5.0}), {}, date(2024, 6, 1))
    assert [(o.side, o.ticker) for o in orders] == [("sell", "AAPL.US")]


class _BuysOnEmptyPicks(BaseStrategy):
    id = "buys_on_empty"

    def estimate_return(self, ticker, as_of, lake):
        return 1.0

    def decide(self, my_picks, portfolio, prices, as_of):
        return [
            Order(client_id="b", ticker="NEW.US", side="buy", quantity=1.0),
            Order(client_id="s", ticker="OLD.US", side="sell", quantity=1.0),
        ]


def test_inner_exit_mode_keeps_only_the_inner_strategys_sells(lake):
    f = _filter(
        inner_class_path=f"{__name__}:_BuysOnEmptyPicks",
        inner_params={},
        risk_off_exit="inner",
    )
    as_of = date(2024, 6, 1)
    f.estimate_return("X.US", as_of, lake)
    portfolio = Portfolio(cash=0.0, positions={"OLD.US": 1.0, "OTHER.US": 3.0})
    orders = f.decide([], portfolio, {}, as_of)
    assert [(o.side, o.ticker) for o in orders] == [("sell", "OLD.US")]


# ---- persistence ----------------------------------------------------------------


def test_save_load_round_trip(tmp_path, lake):
    f = _filter(threshold=0.25)
    f.save(tmp_path / "art")
    loaded = MacroRegimeFilter.load(tmp_path / "art")
    assert loaded.params == f.params
    assert isinstance(loaded.inner, BuyAndHold)
    assert loaded.is_risk_off(date(2024, 6, 1), lake) is True

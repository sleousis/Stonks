"""VIX term-structure regime condition (BL-46)."""

from __future__ import annotations

from datetime import date, datetime

import pandas as pd
import pytest

from stonks.features.regime_conditions import ConditionContext, build_condition, condition_kinds
from stonks.features.regime_vix import VixTermStructureCondition
from stonks.strategies.regime import RegimeFilter


def _rows(indicator, values):
    return [
        {
            "country_iso": "USA",
            "indicator": indicator,
            "observation_date": d,
            "period": None,
            "country_name": "United States",
            "value": v,
        }
        for d, v in values
    ]


@pytest.fixture
def vix_lake(lake):
    days = [date(2020, 2, d) for d in (18, 19, 20, 21, 24, 25)]
    spot = [14.0, 14.5, 15.0, 17.0, 25.0, 30.0]
    term = [17.0, 17.2, 17.5, 18.0, 21.0, 24.0]
    lake.upsert_macro_indicators(
        pd.DataFrame(
            _rows("vix_spot", list(zip(days, spot, strict=True)))
            + _rows("vix_3m", list(zip(days, term, strict=True)))
            + _rows("vix_3m", [(date(2020, 2, 26), None)])
        )
    )
    return lake


def _cond(**kw) -> VixTermStructureCondition:
    return build_condition({"kind": "vix_term_structure", **kw})  # type: ignore[return-value]


def test_registered_through_the_module_name():
    assert condition_kinds()["vix_term_structure"] is VixTermStructureCondition


def test_contango_is_risk_on_and_backwardation_risk_off(vix_lake):
    ctx = ConditionContext(vix_lake)
    cond = _cond()
    assert cond.triggered(date(2020, 2, 21), ctx) is False  # 17 / 18
    assert cond.triggered(datetime(2020, 2, 24), ctx) is True  # 25 / 21
    # the weekend reads Friday's close
    assert cond.triggered(date(2020, 2, 23), ctx) is False


def test_threshold_and_publication_lag(vix_lake):
    ctx = ConditionContext(vix_lake)
    assert _cond(threshold=1.3).triggered(date(2020, 2, 25), ctx) is False  # 1.25
    # with a one-day lag, Monday still sees Friday's contango
    assert _cond(publication_lag_days=1).triggered(date(2020, 2, 24), ctx) is False


def test_missing_or_stale_legs_are_unknown(vix_lake, lake):
    ctx = ConditionContext(vix_lake)
    assert _cond().triggered(date(2020, 2, 1), ctx) is None  # before any data
    assert _cond(max_staleness_days=2).triggered(date(2020, 3, 10), ctx) is None
    assert _cond(term_indicator="nope").triggered(date(2020, 2, 24), ctx) is None


def test_no_look_ahead(vix_lake):
    """A later inversion never changes an earlier answer."""
    ctx = ConditionContext(vix_lake)
    before = _cond().triggered(date(2020, 2, 21), ctx)
    vix_lake.upsert_macro_indicators(pd.DataFrame(_rows("vix_spot", [(date(2020, 2, 22), 99.0)])))
    assert _cond().triggered(date(2020, 2, 21), ConditionContext(vix_lake)) == before


def test_bad_spec_is_rejected():
    with pytest.raises(ValueError):
        _cond(threshold=5.0)


def test_regime_filter_accepts_the_condition():
    f = RegimeFilter(
        {
            "inner_class_path": "tests.unit.test_regime_filter:TwoWayInner",
            "inner_params": {},
            "conditions": [{"kind": "vix_term_structure"}],
        }
    )
    assert isinstance(f.conditions[0], VixTermStructureCondition)

"""Edge cases of the core primitives: date coercion, corporate actions,
interval codes, parameter checks and small value types."""

from __future__ import annotations

from datetime import date, datetime

import numpy as np
import pandas as pd
import pytest

from stonks.core.corporate_actions import (
    CorporateActions,
    Dividend,
    NoCorporateActions,
    Split,
)
from stonks.core.interval import Interval
from stonks.core.params import ParameterSpec, validate_params
from stonks.core.timeutil import as_datetime, day_end, day_start, iso
from stonks.core.types import Features

# ---- timeutil ----------------------------------------------------------------------


def test_as_datetime_takes_dates_timestamps_and_datetimes():
    assert as_datetime(date(2026, 1, 2)) == datetime(2026, 1, 2)
    assert as_datetime(pd.Timestamp("2026-01-02 15:30")) == datetime(2026, 1, 2, 15, 30)
    stamp = datetime(2026, 1, 2, 9)
    assert as_datetime(stamp) is stamp
    assert as_datetime("2026-01-02") == "2026-01-02"  # not a date: handed back


def test_day_start_and_end_bound_a_plain_date():
    assert day_start(date(2026, 1, 2)) == datetime(2026, 1, 2)
    assert day_end(date(2026, 1, 2)) == datetime(2026, 1, 2, 23, 59, 59, 999999)
    stamp = datetime(2026, 1, 2, 9, 30)
    assert day_start(stamp) is stamp and day_end(stamp) is stamp
    assert day_start(None) is None and day_end(None) is None


def test_iso_formats_dates_and_falls_back_to_str():
    assert iso(date(2026, 1, 2)) == "2026-01-02"
    assert iso(42) == "42"


# ---- corporate actions ---------------------------------------------------------------


def test_actions_are_sorted_with_splits_before_dividends_on_one_day():
    day = date(2026, 3, 2)
    actions = CorporateActions.from_events(
        [Dividend("A", day, 0.5), Split("A", day, 2.0), Dividend("A", date(2026, 1, 5), 0.2)]
    )
    kinds = [type(e).__name__ for e in actions.for_ticker("A")]
    assert kinds == ["Dividend", "Split", "Dividend"]
    assert actions.for_ticker("B") == ()
    assert bool(actions) and not bool(CorporateActions())


def test_bad_corporate_actions_are_refused():
    with pytest.raises(ValueError, match="positive"):
        Split("A", date(2026, 1, 2), 0.0)
    with pytest.raises(ValueError, match="non-negative"):
        Dividend("A", date(2026, 1, 2), -1.0)


def test_a_source_without_corporate_actions_loads_none():
    assert not NoCorporateActions().load(["A", "B"])


# ---- interval codes ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "error"),
    [
        (5, TypeError),
        ("   ", ValueError),
        ("5M", ValueError),  # ambiguous: minutes or months
        ("5x", ValueError),
        ("m", ValueError),
        ("0d", ValueError),
    ],
)
def test_bad_interval_codes_are_refused(code, error):
    with pytest.raises(error):
        Interval.parse(code)


# ---- parameters ----------------------------------------------------------------------


def test_numpy_bools_count_as_bools_but_not_as_numbers():
    space = [
        ParameterSpec(name="flag", kind="bool", default=False, bounds=None),
        ParameterSpec(name="n", kind="int", default=1, bounds=(1, 5)),
        ParameterSpec(name="x", kind="float", default=0.5, bounds=None),
    ]
    validate_params({"flag": np.bool_(True), "n": 3, "x": 2.5}, space)
    with pytest.raises(ValueError, match="expected bool"):
        validate_params({"flag": 1}, space)
    with pytest.raises(ValueError, match="expected int"):
        validate_params({"n": True}, space)
    with pytest.raises(ValueError, match="expected float"):
        validate_params({"x": "0.5"}, space)
    with pytest.raises(ValueError, match="out of bounds"):
        validate_params({"n": 9}, space)
    with pytest.raises(ValueError, match="unknown parameter"):
        validate_params({"nope": 1}, space)


def test_categorical_without_choices_takes_any_value():
    space = [
        ParameterSpec(name="ticker", kind="categorical", default="A.US", bounds=None),
        ParameterSpec(name="side", kind="categorical", default="long", bounds=("long", "short")),
    ]
    validate_params({"ticker": "ANY.US", "side": "short"}, space)
    with pytest.raises(ValueError, match="not in choices"):
        validate_params({"side": "flat"}, space)


# ---- small value types ------------------------------------------------------------------


def test_features_act_like_a_read_only_mapping():
    f = Features(values={"a": 1.0, "b": 2.0})
    assert len(f) == 2 and list(f) == ["a", "b"]
    assert f.get("a") == 1.0 and f.get("z", 0.0) == 0.0

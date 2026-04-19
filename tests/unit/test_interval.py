"""Unit tests for core.interval.

An ``Interval`` is a parseable, comparable, hashable value type that names
a bar duration (1m, 5m, 15m, 1h, 4h, 12h, 1d, 1w, …) and exposes both a
canonical code and a ``timedelta``.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from stonks.core.interval import Interval


def test_parse_minutes_hours_days_weeks():
    assert Interval.parse("1m").seconds == 60
    assert Interval.parse("5m").seconds == 300
    assert Interval.parse("15m").seconds == 15 * 60
    assert Interval.parse("1h").seconds == 3600
    assert Interval.parse("4h").seconds == 4 * 3600
    assert Interval.parse("12h").seconds == 12 * 3600
    assert Interval.parse("1d").seconds == 24 * 3600
    assert Interval.parse("1w").seconds == 7 * 24 * 3600


def test_parse_canonical_code_preserved():
    assert Interval.parse("5m").code == "5m"
    assert Interval.parse("4h").code == "4h"
    assert Interval.parse("1d").code == "1d"


def test_to_timedelta_matches_seconds():
    assert Interval.parse("5m").to_timedelta() == timedelta(minutes=5)
    assert Interval.parse("1h").to_timedelta() == timedelta(hours=1)
    assert Interval.parse("1d").to_timedelta() == timedelta(days=1)


def test_str_is_the_code():
    assert str(Interval.parse("15m")) == "15m"
    assert str(Interval.parse("1d")) == "1d"


def test_equality_and_hashability():
    a = Interval.parse("5m")
    b = Interval.parse("5m")
    c = Interval.parse("15m")
    assert a == b
    assert a != c
    assert hash(a) == hash(b)
    s = {a, b, c}
    assert len(s) == 2


def test_parse_normalizes_whitespace_and_case():
    assert Interval.parse(" 5M ") == Interval.parse("5m")


def test_parse_rejects_unknown_unit():
    with pytest.raises(ValueError, match="unit"):
        Interval.parse("5x")


def test_parse_rejects_missing_number():
    with pytest.raises(ValueError):
        Interval.parse("m")


def test_parse_rejects_non_positive_amount():
    with pytest.raises(ValueError):
        Interval.parse("0m")
    with pytest.raises(ValueError):
        Interval.parse("-5m")


def test_well_known_constants_exist_and_match():
    assert Interval.parse("1m") == Interval.MIN_1
    assert Interval.parse("5m") == Interval.MIN_5
    assert Interval.parse("15m") == Interval.MIN_15
    assert Interval.parse("1h") == Interval.HOUR_1
    assert Interval.parse("4h") == Interval.HOUR_4
    assert Interval.parse("1d") == Interval.DAY_1
    assert Interval.parse("1w") == Interval.WEEK_1


def test_is_intraday_helper():
    assert Interval.parse("5m").is_intraday is True
    assert Interval.parse("4h").is_intraday is True
    assert Interval.parse("1d").is_intraday is False
    assert Interval.parse("1w").is_intraday is False

"""How much an economic release matters, and its countries (roadmap 20.9)."""

from __future__ import annotations

import pytest

from stonks.calendars.countries import COUNTRY_LABELS, country_for_currency, is_country_code
from stonks.calendars.importance import (
    IMPORTANCE_LEVELS,
    KeywordImportance,
    at_least,
    default_rater,
)


@pytest.mark.parametrize(
    ("event_type", "expected"),
    [
        ("CPI", "high"),
        ("Core CPI", "high"),
        ("Inflation Rate YoY", "high"),
        ("Non Farm Payrolls", "high"),
        ("Nonfarm Payrolls", "high"),
        ("Fed Interest Rate Decision", "high"),
        ("ECB Interest Rate Decision", "high"),
        ("GDP Growth Rate", "high"),
        ("Unemployment Rate", "high"),
        ("Core PCE Price Index", "high"),
        ("Retail Sales", "medium"),
        ("ISM Manufacturing PMI", "medium"),
        ("PPI", "medium"),
        ("Initial Jobless Claims", "medium"),
        ("Industrial Production", "medium"),
        ("Consumer Confidence", "medium"),
        ("Crude Oil Inventories", "low"),
        ("3-Month Bill Auction", "low"),
        ("Pending Home Sales", "low"),
    ],
)
def test_keyword_rules(event_type, expected):
    assert KeywordImportance().rate(event_type) == expected


def test_words_match_whole_words_only():
    # "cpi" inside another word is not the CPI
    assert KeywordImportance().rate("Occupied Housing Units") == "low"


def test_default_rater_is_the_keyword_rules():
    assert isinstance(default_rater(), KeywordImportance)


def test_threshold_order():
    assert IMPORTANCE_LEVELS == ("low", "medium", "high")
    assert at_least("high", "high")
    assert at_least("high", "low")
    assert at_least("medium", "medium")
    assert not at_least("medium", "high")
    assert not at_least("low", "medium")


@pytest.mark.parametrize(
    ("currency", "country"),
    [("USD", "US"), ("eur", "EU"), ("GBP", "GB"), ("JPY", "JP"), ("CHF", "CH"), ("XYZ", None)],
)
def test_country_for_currency(currency, country):
    assert country_for_currency(currency) == country


def test_country_codes():
    assert is_country_code("US")
    assert is_country_code("EU")
    assert not is_country_code("us")
    assert not is_country_code("USAX")
    assert not is_country_code("")
    assert COUNTRY_LABELS["US"] == "United States"
    assert all(is_country_code(c) for c in COUNTRY_LABELS)

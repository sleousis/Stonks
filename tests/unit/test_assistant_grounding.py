"""The numeric grounding check (roadmap 23.8): every number in a reply must
appear in a tool result of that turn, with tolerant matching."""

from __future__ import annotations

import pytest

from stonks.assistant.grounding import (
    GroundingSettings,
    check_grounding,
    extract_numbers,
    grounded_values,
)


def _ok(reply: str, *payloads, user: str = "", **settings) -> bool:
    report = check_grounding(
        reply, list(payloads), user_texts=[user], settings=GroundingSettings(**settings)
    )
    return report.ok


def test_extract_skips_dates_times_list_markers_and_identifiers():
    text = "1. On 2026-03-20 at 15:30 AAPL.US rose 2.5% (P21, Q3, v2).\n2) Sharpe 1.42"
    values = [m.value for m in extract_numbers(text)]
    assert values == [2.5, 1.42]


def test_extract_reads_units_commas_and_signs():
    [a, b, c, d] = extract_numbers("$1,234.5 then 3.2k, -4.5% and 1.2bn")
    assert a.value == 1234.5 and a.scale == 1
    assert b.value == 3.2 and b.scale == 1_000
    assert c.value == 4.5 and c.percent and c.negative
    assert d.scale == 1_000_000_000


def test_exact_and_rounded_numbers_match():
    payload = {"sharpe": 1.4237, "value": 10523.77}
    assert _ok("Sharpe is 1.42 and the book is worth 10,523.77.", payload)
    assert _ok("Sharpe about 1.4, worth $10,524.", payload)
    assert not _ok("Sharpe is 1.52.", payload)


def test_percent_matches_a_fraction_and_a_percent_field():
    assert _ok("VaR is 1.6% of value.", {"var_95": 0.0164})
    assert _ok("It rose 12.3%.", {"change_pct": 12.34})
    assert not _ok("VaR is 2.6%.", {"var_95": 0.0164})


def test_units_scale_the_value():
    assert _ok("Volume was 1.2M shares.", {"volume": 1_234_567})
    assert _ok("Equity is $10.5k.", {"equity": 10_480.0})
    assert not _ok("Volume was 2.2M.", {"volume": 1_234_567})


def test_sign_words_do_not_break_a_match():
    assert _ok("A loss of 2.1% today.", {"day_return": -0.021})


def test_numbers_in_strings_and_list_lengths_count():
    assert _ok("The rule trimmed it to 4 shares.", {"reason": "clipped to 4.0 shares"})
    assert _ok("You hold 5 positions.", {"positions": [1, 2, 3, 4, 5]})


def test_small_integers_and_user_numbers_are_free():
    assert _ok("Here are 2 ideas.", {})
    assert not _ok("Here are 7 ideas.", {}, free_integers_upto=3)
    assert _ok("You asked about 250 days.", {}, user="show me 250 days")


def test_years_are_free_by_default():
    assert _ok("Since 2024 it did well.", {})
    assert not _ok("Since 2024 it did well.", {}, ignore_years=False)


def test_report_lists_the_ungrounded_mentions():
    report = check_grounding(
        "Sharpe 1.42 and drawdown 9.9%", [{"sharpe": 1.42}], settings=GroundingSettings()
    )
    assert not report.ok
    assert report.ungrounded == ["9.9%"]
    assert report.checked == 2


def test_grounded_values_walks_nested_json_and_text():
    values = grounded_values([{"a": [{"b": 2.5}], "c": "worth 3,000"}, '{"d": 7}'])
    assert {2.5, 3000.0, 7.0} <= values


@pytest.mark.parametrize("mode", ["off", "flag", "rewrite"])
def test_modes_parse(mode):
    assert GroundingSettings(mode=mode).mode == mode

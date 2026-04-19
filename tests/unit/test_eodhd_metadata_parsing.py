"""Unit tests for the extended EODHD metadata parsers.

All work off hand-crafted fixtures mirroring the real response shapes so
tests stay hermetic. The actual HTTP wiring is covered by an injected
fake session in ``test_eodhd_metadata_http``.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

from stonks.ingest.sources.eodhd import (
    parse_analyst_estimates_from_fundamentals,
    parse_analyst_ratings_from_fundamentals,
    parse_dividends_response,
    parse_employee_count_snapshot,
    parse_insider_response,
    parse_news_response,
    parse_profile_from_fundamentals,
    parse_sentiments_response,
    parse_shares_outstanding_snapshot,
)

FIXTURES = Path(__file__).parent.parent / "fixtures" / "eodhd"


def _load(name: str):
    return json.loads((FIXTURES / name).read_text())


# ---- profile ---------------------------------------------------------------


def test_profile_extracts_general_and_shares_and_technicals():
    profile = parse_profile_from_fundamentals("AAPL.US", _load("aapl_fundamentals.json"))
    assert profile is not None
    assert profile.id == "AAPL.US"
    assert profile.name == "Apple Inc"
    assert profile.sector == "Technology"
    assert profile.industry == "Consumer Electronics"
    assert profile.fiscal_year_end == "September"
    assert profile.beta == 1.25
    assert profile.short_percent == 0.6
    assert profile.insider_ownership_percent == 7.0
    assert profile.institutional_ownership_percent == 61.0
    assert profile.employee_count == 164_000
    assert profile.ipo_date == date(1980, 12, 12)


def test_profile_returns_none_for_non_dict_payload():
    assert parse_profile_from_fundamentals("X.US", []) is None
    assert parse_profile_from_fundamentals("X.US", None) is None


# ---- analyst estimates -----------------------------------------------------


def test_analyst_estimates_flatten_earnings_history():
    rows = list(
        parse_analyst_estimates_from_fundamentals(
            "AAPL.US", _load("aapl_fundamentals.json")
        )
    )
    # 2 periods × (epsActual, epsEstimate, epsDifference, surprisePercent) = 8 rows
    assert len(rows) == 8
    metrics = {r.metric for r in rows}
    assert metrics == {"epsActual", "epsEstimate", "epsDifference", "surprisePercent"}
    q4 = [r for r in rows if r.period_end == date(2025, 12, 31)]
    eps_actual = next(r for r in q4 if r.metric == "epsActual")
    assert eps_actual.value == 2.40


# ---- analyst ratings -------------------------------------------------------


def test_analyst_ratings_current_snapshot():
    rating = parse_analyst_ratings_from_fundamentals(
        "AAPL.US", _load("aapl_fundamentals.json")
    )
    assert rating is not None
    assert rating.rating == 2.3
    assert rating.target_price == 250.0
    assert rating.strong_buy == 10
    assert rating.sell == 2


def test_analyst_ratings_none_when_section_absent():
    assert parse_analyst_ratings_from_fundamentals("X.US", {"General": {}}) is None


# ---- shares outstanding + employees snapshots -----------------------------


def test_shares_outstanding_snapshot_stamped_with_as_of():
    row = parse_shares_outstanding_snapshot(
        "AAPL.US", _load("aapl_fundamentals.json"), as_of=date(2026, 4, 15)
    )
    assert row is not None
    assert row.shares == 15_600_000_000.0
    assert row.date == date(2026, 4, 15)


def test_employee_count_snapshot_uses_general_full_time_employees():
    row = parse_employee_count_snapshot(
        "AAPL.US", _load("aapl_fundamentals.json"), as_of=date(2026, 4, 15)
    )
    assert row is not None
    assert row.count == 164_000


# ---- dividends -------------------------------------------------------------


def test_dividends_parse_from_div_endpoint_fixture():
    rows = list(parse_dividends_response("AAPL.US", _load("aapl_dividends.json")))
    assert len(rows) == 2
    row = next(r for r in rows if r.ex_date == date(2026, 2, 10))
    assert row.amount == 0.25
    assert row.currency == "USD"
    assert row.pay_date == date(2026, 2, 13)


def test_dividends_skip_negative_amounts_and_missing_dates():
    payload = [
        {"date": "2026-02-10", "value": 0.25, "currency": "USD"},
        {"date": "2026-03-01", "value": -0.1},     # negative: skipped
        {"value": 0.10, "currency": "USD"},         # missing date: skipped
    ]
    rows = list(parse_dividends_response("AAPL.US", payload))
    assert len(rows) == 1


# ---- news ------------------------------------------------------------------


def test_news_parses_timestamps_and_sentiment_polarity():
    rows = list(parse_news_response("AAPL.US", _load("aapl_news.json")))
    assert len(rows) == 2
    first = rows[0]
    assert first.title == "Apple announces new product line"
    assert first.published_at == datetime(2026, 4, 1, 14, 30, tzinfo=UTC)
    assert first.sentiment == 0.5


def test_news_accepts_missing_sentiment_and_link():
    rows = list(parse_news_response("AAPL.US", _load("aapl_news.json")))
    second = rows[1]
    assert second.sentiment is None
    assert second.url is not None   # fixture has link


# ---- insider transactions --------------------------------------------------


def test_insider_parse_composes_value_from_shares_and_price():
    rows = list(parse_insider_response("AAPL.US", _load("aapl_insider.json")))
    assert len(rows) == 1
    r = rows[0]
    assert r.date == date(2026, 3, 1)
    assert r.owner_name == "COOK TIMOTHY D"
    assert r.transaction_code == "S"
    assert r.shares == 50000
    assert r.price == 250.0
    assert r.value == 12_500_000.0


# ---- sentiments ------------------------------------------------------------


def test_sentiments_parse_dict_keyed_by_ticker():
    rows = list(
        parse_sentiments_response("AAPL.US", _load("aapl_sentiments.json"))
    )
    assert len(rows) == 2
    first = next(r for r in rows if r.date == date(2026, 4, 1))
    assert first.sentiment == 0.2
    assert first.article_count == 12

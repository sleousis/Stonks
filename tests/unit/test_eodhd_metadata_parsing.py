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
    parse_analyst_forecasts_from_fundamentals,
    parse_analyst_ratings_from_fundamentals,
    parse_cross_listings_from_fundamentals,
    parse_dividends_response,
    parse_earnings_announcements_from_fundamentals,
    parse_employee_count_snapshot,
    parse_esg_from_fundamentals,
    parse_holders_from_fundamentals,
    parse_insider_response,
    parse_market_cap_response,
    parse_news_response,
    parse_officers_from_fundamentals,
    parse_profile_from_fundamentals,
    parse_sentiments_response,
    parse_shares_outstanding_history,
    parse_splits_response,
    parse_ticker_snapshot_from_fundamentals,
)

FIXTURES = Path(__file__).parent.parent / "fixtures" / "eodhd"


def _load(name: str):
    return json.loads((FIXTURES / name).read_text())


# ---- profile (static metadata) ---------------------------------------------


def test_profile_extracts_static_metadata_only():
    profile = parse_profile_from_fundamentals("AAPL.US", _load("aapl_fundamentals.json"))
    assert profile is not None
    assert profile.id == "AAPL.US"
    assert profile.name == "Apple Inc"
    assert profile.sector == "Technology"
    assert profile.industry == "Consumer Electronics"
    assert profile.gic_sector == "Information Technology"
    assert profile.gic_group == "Technology Hardware & Equipment"
    assert profile.fiscal_year_end == "September"
    assert profile.ipo_date == date(1980, 12, 12)
    # External identifiers
    assert profile.cusip == "037833100"
    assert profile.cik == "0000320193"
    assert profile.isin == "US0378331005"
    assert profile.open_figi == "BBG000B9XRY4"
    assert profile.lei == "HWUPKR0MPOU8FGXBT394"
    # Address
    assert profile.address_city == "Cupertino"
    assert profile.address_country == "USA"
    # Normalized security_type — vendor "Common Stock" → "common_stock"
    assert profile.security_type == "common_stock"
    # Description present
    assert profile.description is not None and "designs" in profile.description


def test_profile_returns_none_for_non_dict_payload():
    assert parse_profile_from_fundamentals("X.US", []) is None
    assert parse_profile_from_fundamentals("X.US", None) is None


def test_profile_normalizes_security_type_for_etf_and_other():
    payload = {"General": {"Type": "ETF"}}
    assert parse_profile_from_fundamentals("X.US", payload).security_type == "etf"
    payload = {"General": {"Type": "Mutual Fund"}}
    assert parse_profile_from_fundamentals("X.US", payload).security_type == "fund"
    payload = {"General": {"Type": "Some Weird Vehicle"}}
    assert parse_profile_from_fundamentals("X.US", payload).security_type == "other"
    # Empty / missing
    assert parse_profile_from_fundamentals("X.US", {"General": {}}).security_type is None


def test_profile_picks_up_delisted_date_when_present():
    payload = {"General": {"IsDelisted": True, "DelistedDate": "2008-09-17"}}
    profile = parse_profile_from_fundamentals("LEH.US", payload)
    assert profile is not None
    assert profile.is_delisted is True
    assert profile.delisted_date == date(2008, 9, 17)


# ---- ticker snapshot (volatile metrics) ------------------------------------


def test_ticker_snapshot_extracts_volatile_metrics():
    snap = parse_ticker_snapshot_from_fundamentals(
        "AAPL.US",
        _load("aapl_fundamentals.json"),
        as_of=date(2026, 4, 27),
    )
    assert snap is not None
    assert snap.ticker == "AAPL.US"
    assert snap.snapshot_date == date(2026, 4, 27)
    assert snap.beta == 1.25
    assert snap.short_percent == 0.6
    assert snap.percent_insiders == 7.0
    assert snap.percent_institutions == 61.0


def test_ticker_snapshot_returns_none_when_all_metrics_absent():
    assert (
        parse_ticker_snapshot_from_fundamentals(
            "X.US", {"General": {}, "SharesStats": {}, "Technicals": {}}
        )
        is None
    )


# ---- earnings announcements (replaces flat analyst_estimates) -------------


def test_earnings_announcements_extract_event_with_normalized_market_window():
    rows = list(
        parse_earnings_announcements_from_fundamentals("AAPL.US", _load("aapl_fundamentals.json"))
    )
    assert len(rows) == 2
    q4 = next(r for r in rows if r.period_end == date(2025, 12, 31))
    assert q4.report_date == date(2026, 1, 25)
    # Vendor "AfterMarket" → canonical "after"
    assert q4.before_after_market == "after"
    assert q4.eps_actual == 2.40
    assert q4.eps_estimate == 2.35
    assert q4.surprise_percent == 2.13


def test_earnings_announcements_handle_unknown_market_window_as_none():
    payload = {
        "Earnings": {
            "History": {
                "2026-03-31": {
                    "date": "2026-03-31",
                    "beforeAfterMarket": "Unknown",
                    "epsEstimate": 1.94,
                }
            }
        }
    }
    rows = list(parse_earnings_announcements_from_fundamentals("X.US", payload))
    assert len(rows) == 1
    assert rows[0].before_after_market is None


# ---- analyst forecasts (Earnings.Trend) ------------------------------------


def test_analyst_forecasts_normalize_period_and_capture_revisions():
    rows = list(
        parse_analyst_forecasts_from_fundamentals("AAPL.US", _load("aapl_fundamentals.json"))
    )
    assert len(rows) == 2
    cur_q = next(r for r in rows if r.period_end == date(2026, 3, 31))
    # Vendor "0q" → canonical "current_quarter"
    assert cur_q.period_relative == "current_quarter"
    assert cur_q.eps_estimate_avg == 1.94
    assert cur_q.eps_estimate_low == 1.85
    assert cur_q.eps_estimate_high == 2.05
    assert cur_q.eps_estimate_n_analysts == 32
    assert cur_q.revenue_estimate_avg == 94_000_000_000.0
    assert cur_q.eps_revisions_up_30d == 2

    next_y = next(r for r in rows if r.period_end == date(2027, 9, 30))
    # Vendor "+1y" → canonical "next_year"
    assert next_y.period_relative == "next_year"
    assert next_y.eps_revisions_down_7d is None  # vendor null
    assert next_y.eps_revisions_down_30d == 6


# ---- analyst ratings (current snapshot, time-series via snapshot_date) ----


def test_analyst_ratings_carry_explicit_snapshot_date():
    rating = parse_analyst_ratings_from_fundamentals(
        "AAPL.US",
        _load("aapl_fundamentals.json"),
        as_of=date(2026, 4, 27),
    )
    assert rating is not None
    assert rating.snapshot_date == date(2026, 4, 27)
    assert rating.rating == 2.3
    assert rating.target_price == 250.0
    assert rating.strong_buy == 10
    assert rating.sell == 2


def test_analyst_ratings_none_when_section_absent():
    assert parse_analyst_ratings_from_fundamentals("X.US", {"General": {}}) is None


# ---- holders (institutions + funds) ---------------------------------------


def test_holders_extract_institutions_and_funds_with_kind_discriminator():
    rows = list(parse_holders_from_fundamentals("AAPL.US", _load("aapl_fundamentals.json")))
    # 2 institutions + 2 funds in the fixture
    assert len(rows) == 4
    institutions = [r for r in rows if r.holder_kind == "institution"]
    funds = [r for r in rows if r.holder_kind == "fund"]
    assert len(institutions) == 2
    assert len(funds) == 2
    vanguard = next(r for r in rows if r.name == "Vanguard Group Inc")
    assert vanguard.snapshot_date == date(2025, 12, 31)
    assert vanguard.total_shares_pct == 9.7151
    assert vanguard.current_shares == 1_426_283_914
    assert vanguard.change_pct == 1.9191


def test_holders_empty_when_section_missing():
    assert list(parse_holders_from_fundamentals("X.US", {"General": {}})) == []


# ---- ESG (snapshot + activities) -------------------------------------------


def test_esg_parses_snapshot_and_activities():
    snap, activities = parse_esg_from_fundamentals("AAPL.US", _load("aapl_fundamentals.json"))
    assert snap is not None
    assert snap.rating_date == date(2026, 4, 1)
    assert snap.total_esg == 17.04
    assert snap.environment_score == 0.7
    assert snap.governance_percentile == 56
    assert snap.controversy_level == 3
    # Five activities in the fixture
    assert len(activities) == 5
    alc = next(a for a in activities if a.activity == "alcohol")
    assert alc.involvement == "No"


def test_esg_returns_empty_pair_when_section_missing():
    snap, activities = parse_esg_from_fundamentals("X.US", {"General": {}})
    assert snap is None
    assert activities == []


def test_esg_returns_empty_pair_when_rating_date_missing():
    snap, activities = parse_esg_from_fundamentals("X.US", {"ESGScores": {"TotalEsg": 10}})
    assert snap is None
    assert activities == []


# ---- cross listings --------------------------------------------------------


def test_cross_listings_extract_other_exchange_listings():
    rows = list(parse_cross_listings_from_fundamentals("AAPL.US", _load("aapl_fundamentals.json")))
    assert len(rows) == 2
    lse = next(r for r in rows if r.exchange == "LSE")
    assert lse.exchange_code == "0R2V"


# ---- officers (current roster) ---------------------------------------------


def test_officers_extract_current_roster_with_year_born():
    rows = list(parse_officers_from_fundamentals("AAPL.US", _load("aapl_fundamentals.json")))
    assert len(rows) == 3
    cook = next(r for r in rows if "Timothy" in r.name)
    assert cook.title == "CEO & Director"
    assert cook.year_born == 1961


# ---- shares outstanding + employees snapshots -----------------------------


def test_shares_outstanding_history_from_fundamentals():
    rows = list(parse_shares_outstanding_history("AAPL.US", _load("aapl_fundamentals.json")))
    # fixture has 2 annual + 3 quarterly entries (5 rows — duplicate dates
    # across annual/quarterly are preserved; the lake's ON CONFLICT dedupes).
    assert len(rows) == 5
    assert all(r.ticker == "AAPL.US" for r in rows)
    dates = {r.date for r in rows}
    assert date(2025, 12, 31) in dates
    assert date(2025, 9, 30) in dates
    # integer share counts are preserved as floats (lake schema is DOUBLE)
    assert all(r.shares > 0 for r in rows)


def test_shares_outstanding_history_empty_when_section_missing():
    rows = list(parse_shares_outstanding_history("X.US", {"General": {}}))
    assert rows == []


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
        {"date": "2026-03-01", "value": -0.1},  # negative: skipped
        {"value": 0.10, "currency": "USD"},  # missing date: skipped
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
    assert second.url is not None  # fixture has link


# ---- insider transactions --------------------------------------------------


def test_insider_parse_extracts_full_form4_surface():
    rows = list(parse_insider_response("AAPL.US", _load("aapl_insider.json")))
    assert len(rows) == 2
    cook = next(r for r in rows if r.owner_name == "COOK TIMOTHY D")
    assert cook.transaction_date == date(2026, 3, 1)
    assert cook.filing_date == date(2026, 3, 5)
    assert cook.owner_cik == "0001214156"
    assert cook.owner_relation == "Chief Executive Officer"
    assert cook.owner_title == "CEO"
    assert cook.transaction_code == "S"
    assert cook.acquired_disposed == "D"
    assert cook.shares == 50_000.0
    assert cook.price == 250.0
    assert cook.value == 12_500_000.0  # derived shares * price
    assert cook.post_transaction_amount == 3_200_000.0
    assert cook.sec_link is not None and cook.sec_link.startswith("https://sec.gov/")


def test_insider_parse_handles_partial_rows():
    rows = list(parse_insider_response("AAPL.US", _load("aapl_insider.json")))
    luca = next(r for r in rows if r.owner_name == "MAESTRI LUCA")
    assert luca.owner_cik is None
    assert luca.owner_title == "CFO"
    assert luca.acquired_disposed == "A"
    assert luca.sec_link is None
    assert luca.post_transaction_amount is None


def test_insider_parse_drops_rows_without_transaction_date():
    rows = list(
        parse_insider_response(
            "X.US",
            [
                {"ownerName": "X", "transactionAmount": 100},  # no transactionDate / date
            ],
        )
    )
    assert rows == []


# ---- sentiments ------------------------------------------------------------


def test_sentiments_parse_dict_keyed_by_ticker():
    rows = list(parse_sentiments_response("AAPL.US", _load("aapl_sentiments.json")))
    assert len(rows) == 2
    first = next(r for r in rows if r.date == date(2026, 4, 1))
    assert first.sentiment == 0.2
    assert first.article_count == 12


# ---- stock splits ----------------------------------------------------------


def test_splits_parse_ratio_from_new_over_old_string():
    rows = list(parse_splits_response("AAPL.US", _load("aapl_splits.json")))
    assert len(rows) == 5
    # 2014 was the 7-for-1 split
    split_7_1 = next(r for r in rows if r.date == date(2014, 6, 9))
    assert split_7_1.ratio == 7.0
    # 2020 was the 4-for-1
    split_4_1 = next(r for r in rows if r.date == date(2020, 8, 31))
    assert split_4_1.ratio == 4.0


def test_splits_parser_skips_malformed_rows():
    rows = list(
        parse_splits_response(
            "X.US",
            [
                {"date": "2020-01-01", "split": "not-a-ratio"},
                {"date": "bad-date", "split": "2/1"},
                {"date": "2020-02-01", "split": "2/0"},  # divide-by-zero
                {"date": "2020-03-01", "split": "2/1"},  # only valid row
            ],
        )
    )
    assert len(rows) == 1
    assert rows[0].ratio == 2.0


# ---- historical market cap -------------------------------------------------


def test_market_cap_parses_dict_keyed_by_index():
    rows = list(parse_market_cap_response("AAPL.US", _load("aapl_market_cap.json")))
    assert len(rows) == 4
    assert all(r.ticker == "AAPL.US" for r in rows)
    top = next(r for r in rows if r.date == date(2025, 12, 31))
    assert top.market_cap == 3_800_000_000_000.0


def test_market_cap_parser_accepts_list_shape_too():
    payload = [
        {"date": "2025-01-01", "value": 1000000000},
        {"date": "2025-01-08", "value": 1050000000},
    ]
    rows = list(parse_market_cap_response("X.US", payload))
    assert len(rows) == 2

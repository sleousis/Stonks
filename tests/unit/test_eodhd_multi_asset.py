"""Unit tests for the EODHD multi-asset hooks: ticker → asset_class
classification, and per-class profile parsers.

Pure functions only — no HTTP, no captured fixtures larger than what
makes the test legible. Network-shape tests live in test_eodhd_metadata_http.py.
"""

import pytest

from stonks.ingest.sources.eodhd import (
    classify_asset_class,
    parse_bond_profile_response,
    parse_commodity_contract_response,
    parse_crypto_profile_response,
)


# ---- classify_asset_class --------------------------------------------------


@pytest.mark.parametrize(
    ("ticker", "expected"),
    [
        ("AAPL.US", "equity"),
        ("MSFT.US", "equity"),
        ("VOD.LSE", "equity"),
        ("SAP.XETRA", "equity"),
        ("BTC-USD.CC", "crypto"),
        ("ETH-USD.CC", "crypto"),
        ("DOGE-USD.CC", "crypto"),
        ("GC.COMM", "commodity"),
        ("CL.COMM", "commodity"),
        ("SI.COMM", "commodity"),
        ("US10Y.GBOND", "bond"),
        ("DE10Y.GBOND", "bond"),
    ],
)
def test_classify_asset_class_by_suffix(ticker, expected):
    assert classify_asset_class(ticker) == expected


def test_classify_asset_class_is_case_insensitive_on_suffix():
    """Vendor symbols are typically uppercase, but adapters shouldn't trip
    over a lower-cased suffix that slips in from a config file or CLI."""
    assert classify_asset_class("BTC-USD.cc") == "crypto"
    assert classify_asset_class("GC.comm") == "commodity"


def test_classify_asset_class_defaults_unknown_suffix_to_equity():
    """The .US-style equity suffix is open-ended (XETRA, LSE, F, …); only
    the three new explicit suffixes (.CC / .COMM / .GBOND) reroute the
    classification. Everything else stays equity, matching the migration
    backfill of pre-existing data."""
    assert classify_asset_class("UNKNOWN.XYZ") == "equity"
    assert classify_asset_class("AAPL") == "equity"  # no suffix at all


# ---- crypto profile parser -------------------------------------------------


def test_parse_crypto_profile_response_extracts_supply_and_blockchain():
    """EODHD's crypto fundamentals payload exposes supply + chain metadata
    under a Components / General mix; the parser pulls just the fields we
    model and lower-cases the consensus type into the canonical literal."""
    payload = {
        "General": {
            "Code": "BTC-USD",
            "Type": "Currency",
            "Name": "Bitcoin USD",
            "CurrencyCode": "USD",
        },
        "Components": {
            "Blockchain": "Bitcoin",
            "ConsensusType": "Proof-of-Work",
            "CirculatingSupply": 19_700_000.0,
            "TotalSupply": 19_700_000.0,
            "MaxSupply": 21_000_000.0,
        },
    }
    row = parse_crypto_profile_response("BTC-USD.CC", payload)
    assert row is not None
    assert row.ticker == "BTC-USD.CC"
    assert row.base_symbol == "BTC"
    assert row.quote_symbol == "USD"
    assert row.blockchain == "Bitcoin"
    assert row.consensus_type == "proof_of_work"
    assert row.circulating_supply == 19_700_000.0
    assert row.max_supply == 21_000_000.0


def test_parse_crypto_profile_response_handles_missing_components():
    """Free-tier and limited responses may lack the supply block; the
    parser still produces a row keyed on the ticker so identity is
    captured even when the metrics aren't."""
    payload = {"General": {"Code": "DOGE-USD", "Type": "Currency"}}
    row = parse_crypto_profile_response("DOGE-USD.CC", payload)
    assert row is not None
    assert row.ticker == "DOGE-USD.CC"
    assert row.circulating_supply is None
    assert row.max_supply is None


def test_parse_crypto_profile_response_returns_none_for_non_dict_payload():
    assert parse_crypto_profile_response("BTC-USD.CC", []) is None
    assert parse_crypto_profile_response("BTC-USD.CC", "blocked") is None


def test_parse_crypto_profile_response_normalizes_unknown_consensus_to_other():
    payload = {"Components": {"ConsensusType": "Quantum-Goblin-Magic"}}
    row = parse_crypto_profile_response("X-USD.CC", payload)
    assert row is not None
    assert row.consensus_type == "other"


# ---- bond profile parser ---------------------------------------------------


def test_parse_bond_profile_response_extracts_terms():
    payload = {
        "General": {
            "Code": "US10Y",
            "Name": "United States 10-Year Treasury",
            "CurrencyCode": "USD",
        },
        "BondData": {
            "Issuer": "United States Treasury",
            "IssuerType": "Sovereign",
            "BondType": "Treasury",
            "CouponRate": 4.25,
            "CouponFrequency": 2,
            "FaceValue": 1000.0,
            "IssueDate": "2025-11-15",
            "MaturityDate": "2035-11-15",
            "CreditRating": "AAA",
        },
    }
    row = parse_bond_profile_response("US10Y.GBOND", payload)
    assert row is not None
    assert row.issuer_name == "United States Treasury"
    assert row.issuer_kind == "sovereign"
    assert row.bond_kind == "treasury"
    assert row.coupon_rate == 4.25
    assert row.coupon_frequency == 2
    assert row.credit_rating == "AAA"


def test_parse_bond_profile_response_returns_minimal_row_when_data_missing():
    """For tickers EODHD has registered but publishes no bond-specific
    fields for, the parser still yields a ticker-only row so the
    instrument exists in our profile surface."""
    payload = {"General": {"Code": "US10Y", "Type": "Bond"}}
    row = parse_bond_profile_response("US10Y.GBOND", payload)
    assert row is not None
    assert row.ticker == "US10Y.GBOND"
    assert row.issuer_kind is None
    assert row.coupon_rate is None


def test_parse_bond_profile_response_returns_none_for_non_dict_payload():
    assert parse_bond_profile_response("US10Y.GBOND", []) is None


# ---- commodity contract parser --------------------------------------------


def test_parse_commodity_contract_response_extracts_contract_metadata():
    payload = {
        "General": {"Code": "GC", "Name": "Gold Continuous"},
        "ContractData": {
            "UnderlyingSymbol": "GC",
            "ContractType": "Continuous",
            "ContractSize": 100.0,
            "ContractUnit": "troy_ounce",
        },
    }
    row = parse_commodity_contract_response("GC.COMM", payload)
    assert row is not None
    assert row.ticker == "GC.COMM"
    assert row.underlying_symbol == "GC"
    assert row.contract_kind == "continuous"
    assert row.contract_size == 100.0
    assert row.contract_unit == "troy_ounce"


def test_parse_commodity_contract_response_normalizes_unknown_contract_kind_to_other():
    payload = {"ContractData": {"ContractType": "Spread Combo"}}
    row = parse_commodity_contract_response("X.COMM", payload)
    assert row is not None
    assert row.contract_kind == "other"


def test_parse_commodity_contract_response_returns_none_for_non_dict_payload():
    assert parse_commodity_contract_response("GC.COMM", "no-data") is None

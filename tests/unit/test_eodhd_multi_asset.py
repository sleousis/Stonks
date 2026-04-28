"""Unit tests for the EODHD multi-asset hooks: ticker → asset_class
classification, and per-class profile parsers.

Pure functions only — no HTTP, no captured fixtures larger than what
makes the test legible. Network-shape tests live in test_eodhd_metadata_http.py.
"""

import pytest

from stonks.ingest.sources.eodhd import (
    classify_asset_class,
    eodhd_exchange_for_asset_class,
    parse_bond_profile_response,
    parse_commodity_contract_response,
    parse_crypto_profile_response,
)

# ---- eodhd_exchange_for_asset_class ---------------------------------------


@pytest.mark.parametrize(
    ("asset_class", "expected"),
    [
        ("crypto", "CC"),
        ("commodity", "COMM"),
        ("bond", "GBOND"),
    ],
)
def test_eodhd_exchange_for_asset_class_returns_virtual_exchange(asset_class, expected):
    """The non-equity classes each map to exactly one EODHD virtual
    exchange; the inverse must be the round-trip of ``classify_asset_class``
    on a synthetic ticker assembled from the suffix."""
    assert eodhd_exchange_for_asset_class(asset_class) == expected
    assert classify_asset_class(f"FOO.{expected}") == asset_class


def test_eodhd_exchange_for_asset_class_returns_none_for_equity():
    """Equity instruments live on dozens of real exchanges, so the
    resolver returns ``None`` to force callers to ask explicitly."""
    assert eodhd_exchange_for_asset_class("equity") is None


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


@pytest.mark.parametrize(
    ("ticker", "expected"),
    [
        ("", "equity"),  # empty string — degenerate but contractually defined
        ("   ", "equity"),  # all whitespace, no dot
        ("AAPL.US.", "equity"),  # trailing dot → empty suffix
        (".US", "equity"),  # leading dot only
        ("AAPL..US", "equity"),  # double dot — rsplit takes the last segment
        ("BTC.USD.CC", "crypto"),  # multiple dots — rsplit picks ".CC"
        (".CC", "crypto"),  # bare suffix
    ],
)
def test_classify_asset_class_edge_cases(ticker, expected):
    """Pin the contract for tickers that arrive from config / CLI / API
    responses. None of these are *intended* shapes, but the function lives
    at a system boundary and the behaviour shouldn't change silently."""
    assert classify_asset_class(ticker) == expected


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


def test_parse_crypto_profile_response_returns_none_for_unknown_consensus():
    """Unknown vendor strings collapse to ``None`` (and are logged) so
    we can extend the keyword table — distinct from the explicit
    ``\"other\"`` literal which we emit when EODHD literally said
    ``Other``."""
    payload = {"Components": {"ConsensusType": "Quantum-Goblin-Magic"}}
    row = parse_crypto_profile_response("X-USD.CC", payload)
    assert row is not None
    assert row.consensus_type is None


def test_parse_crypto_profile_response_passes_through_explicit_other():
    payload = {"Components": {"ConsensusType": "Other"}}
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


def test_parse_commodity_contract_response_returns_none_for_unknown_contract_kind():
    payload = {"ContractData": {"ContractType": "Spread Combo"}}
    row = parse_commodity_contract_response("X.COMM", payload)
    assert row is not None
    assert row.contract_kind is None


def test_parse_commodity_contract_response_returns_none_for_non_dict_payload():
    assert parse_commodity_contract_response("GC.COMM", "no-data") is None


# ---- _fetch_metadata_non_equity orchestration ------------------------------


class _StubSession:
    """Minimal ``requests.Session`` stand-in: deterministic JSON return
    or raise on ``get``. Used to drive ``_fetch_metadata_non_equity``
    without hitting the network."""

    def __init__(self, response_json=None, raise_exc=None):
        self._response_json = response_json
        self._raise_exc = raise_exc

    def get(self, url, params=None, timeout=None):  # noqa: ARG002
        if self._raise_exc is not None:
            raise self._raise_exc
        return _StubResponse(self._response_json)


class _StubResponse:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200
        self.text = ""

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


def _make_source(session):
    from stonks.ingest.sources.eodhd import EodhdDataSource

    return EodhdDataSource(
        api_key="test", session=session, max_retries=1, retry_backoff_seconds=0.0
    )


def test_fetch_metadata_non_equity_crypto_dispatch_populates_profile():
    """A crypto fundamentals payload routes to ``parse_crypto_profile_response``
    *and* the returned bundle's profile carries ``asset_class='crypto'``
    (the ``model_copy`` override fires regardless of what the equity
    profile parser made of the General block)."""
    src = _make_source(
        _StubSession(
            response_json={
                "General": {"Code": "BTC-USD", "Type": "Currency", "CurrencyCode": "USD"},
                "Components": {
                    "Blockchain": "Bitcoin",
                    "ConsensusType": "Proof-of-Work",
                    "CirculatingSupply": 19_700_000.0,
                    "MaxSupply": 21_000_000.0,
                },
            }
        )
    )
    bundle = src.fetch_metadata("BTC-USD.CC")

    assert bundle.crypto_profile is not None
    assert bundle.crypto_profile.consensus_type == "proof_of_work"
    assert bundle.crypto_profile.max_supply == 21_000_000.0
    assert bundle.profile is not None
    assert bundle.profile.asset_class == "crypto"
    assert bundle.profile.security_type is None  # equity-only sub-kind cleared
    # equity-shaped fields stay empty
    assert bundle.dividends == ()
    assert bundle.bond_profile is None
    assert bundle.commodity_contract is None


def test_fetch_metadata_non_equity_free_tier_yields_minimal_profile():
    """Free-tier blocks return a profile-only bundle keyed on the
    ticker, mirroring the equity path's free-tier behaviour."""
    from stonks.ingest.sources.eodhd import EodhdFreeTierError

    src = _make_source(_StubSession(raise_exc=EodhdFreeTierError("blocked on free tier")))
    bundle = src.fetch_metadata("BTC-USD.CC")
    assert bundle.profile is not None
    assert bundle.profile.id == "BTC-USD.CC"
    assert bundle.profile.asset_class == "crypto"
    assert bundle.crypto_profile is None


def test_fetch_metadata_non_equity_transport_error_escalates():
    """A network outage on the sole non-equity endpoint must surface as
    ``EodhdAllEndpointsFailedError`` so the pipeline records the
    ticker as failed (instead of silently incrementing tickers_ok with
    an empty profile — the regression review C1 caught)."""
    import requests

    from stonks.ingest.sources.eodhd import EodhdAllEndpointsFailedError

    src = _make_source(_StubSession(raise_exc=requests.ConnectionError("boom")))
    with pytest.raises(EodhdAllEndpointsFailedError, match="non-equity"):
        src.fetch_metadata("BTC-USD.CC")


def test_fetch_metadata_non_equity_bond_dispatch_populates_profile():
    src = _make_source(
        _StubSession(
            response_json={
                "General": {"Code": "US10Y", "CurrencyCode": "USD"},
                "BondData": {
                    "Issuer": "United States Treasury",
                    "IssuerType": "Sovereign",
                    "BondType": "Treasury",
                    "CouponRate": 4.25,
                },
            }
        )
    )
    bundle = src.fetch_metadata("US10Y.GBOND")
    assert bundle.bond_profile is not None
    assert bundle.bond_profile.issuer_kind == "sovereign"
    assert bundle.profile is not None
    assert bundle.profile.asset_class == "bond"
    assert bundle.profile.security_type is None
    assert bundle.crypto_profile is None
    assert bundle.commodity_contract is None


def test_fetch_metadata_non_equity_commodity_dispatch_populates_profile():
    src = _make_source(
        _StubSession(
            response_json={
                "General": {"Code": "GC", "Name": "Gold Continuous"},
                "ContractData": {
                    "UnderlyingSymbol": "GC",
                    "ContractType": "Continuous",
                    "ContractSize": 100.0,
                    "ContractUnit": "troy_ounce",
                },
            }
        )
    )
    bundle = src.fetch_metadata("GC.COMM")
    assert bundle.commodity_contract is not None
    assert bundle.commodity_contract.contract_kind == "continuous"
    assert bundle.profile is not None
    assert bundle.profile.asset_class == "commodity"
    assert bundle.profile.security_type is None
    assert bundle.crypto_profile is None
    assert bundle.bond_profile is None

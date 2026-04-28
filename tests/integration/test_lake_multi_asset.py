"""Integration tests for the multi-asset lake surface — migration 007 +
the four new per-class profile upsert methods.

Uses a temp on-disk DB so we exercise the actual migration path."""

from datetime import date

import pandas as pd
import pytest

from stonks.ingest.schemas import (
    BondProfileRow,
    BondYieldRow,
    CommodityContractRow,
    CryptoProfileRow,
    TickerProfile,
)
from stonks.store.lake import DuckDBLake


@pytest.fixture
def lake(tmp_path):
    lake = DuckDBLake(tmp_path / "lake.duckdb")
    lake.migrate()
    yield lake
    lake.close()


def _rows_to_df(rows):
    return pd.DataFrame([r.model_dump() for r in rows])


# ---- migration 007 ---------------------------------------------------------


def test_migration_007_renames_tickers_to_instruments(lake):
    tables = set(lake.tables())
    assert "instruments" in tables
    assert "tickers" not in tables


def test_migration_007_adds_asset_class_column_with_equity_default(lake):
    cols = lake.sql("PRAGMA table_info(instruments)")
    column_names = set(cols["name"].tolist())
    assert "asset_class" in column_names
    # Insert a profile without specifying asset_class — Pydantic default
    # is 'equity', so the row lands as equity.
    profile = TickerProfile(id="AAPL.US", name="Apple Inc")
    assert profile.asset_class == "equity"
    lake.upsert_instrument_profile(_rows_to_df([profile]))
    got = lake.sql("SELECT id, asset_class FROM instruments WHERE id = 'AAPL.US'")
    assert got.iloc[0]["asset_class"] == "equity"


def test_migration_007_backfills_existing_rows_to_equity(tmp_path):
    """The most production-relevant scenario: a lake created on an older
    version (migrations 001-006) that already has rows in ``tickers``,
    then 007 lands. The ``UPDATE … WHERE asset_class IS NULL`` is the
    only thing protecting existing data from landing as NULL — pin it.
    """
    import duckdb

    from stonks.store.lake import MIGRATIONS_DIR

    db_path = tmp_path / "lake.duckdb"
    # Apply migrations 001-006 directly, bypassing the migrate() helper —
    # that's what mirrors the upgrade-an-existing-lake scenario.
    pre_007 = sorted(MIGRATIONS_DIR.glob("00[1-6]_*.sql"))
    assert len(pre_007) == 6
    con = duckdb.connect(str(db_path))
    try:
        con.execute("SET TimeZone = 'UTC'")
        con.execute(
            "CREATE TABLE schema_migrations ("
            " version INTEGER PRIMARY KEY, applied_at TIMESTAMP NOT NULL)"
        )
        for sql_path in pre_007:
            con.execute(sql_path.read_text())
            con.execute(
                "INSERT INTO schema_migrations VALUES (?, current_timestamp)",
                [int(sql_path.stem.split("_", 1)[0])],
            )
        # Insert a pre-007 row into ``tickers``.
        con.execute(
            "INSERT INTO tickers (id, exchange, currency, name) "
            "VALUES ('AAPL.US', 'US', 'USD', 'Apple Inc')"
        )
    finally:
        con.close()

    # Now run the full migrate() — it should apply 007, do the rename
    # plus backfill, and leave the existing row classified as equity.
    lake = DuckDBLake(db_path)
    try:
        lake.migrate()

        tables = set(lake.tables())
        assert "instruments" in tables
        assert "tickers" not in tables

        row = lake.sql("SELECT id, asset_class, name FROM instruments").iloc[0]
        assert row["id"] == "AAPL.US"
        assert row["asset_class"] == "equity"
        assert row["name"] == "Apple Inc"

        # Round-trip through the same helper the Ranker uses.
        assert lake.get_asset_classes(["AAPL.US"]) == {"AAPL.US": "equity"}
    finally:
        lake.close()


def test_instrument_profile_round_trip_preserves_asset_class(lake):
    crypto = TickerProfile(id="BTC-USD.CC", asset_class="crypto", name="Bitcoin")
    bond = TickerProfile(id="US10Y.GBOND", asset_class="bond")
    commodity = TickerProfile(id="GC.COMM", asset_class="commodity")
    equity = TickerProfile(id="AAPL.US")  # default equity

    lake.upsert_instrument_profile(_rows_to_df([crypto, bond, commodity, equity]))

    got = lake.sql("SELECT id, asset_class FROM instruments ORDER BY id")
    assert got.set_index("id")["asset_class"].to_dict() == {
        "AAPL.US": "equity",
        "BTC-USD.CC": "crypto",
        "GC.COMM": "commodity",
        "US10Y.GBOND": "bond",
    }


# ---- crypto profiles -------------------------------------------------------


def test_upsert_crypto_profile_round_trip(lake):
    row = CryptoProfileRow(
        ticker="BTC-USD.CC",
        base_symbol="BTC",
        quote_symbol="USD",
        blockchain="Bitcoin",
        consensus_type="proof_of_work",
        circulating_supply=19_700_000.0,
        max_supply=21_000_000.0,
        supply_snapshot_date=date(2026, 4, 1),
    )
    assert lake.upsert_crypto_profile(_rows_to_df([row])) == 1
    got = lake.sql("SELECT * FROM crypto_profiles WHERE ticker = 'BTC-USD.CC'")
    assert got.iloc[0]["base_symbol"] == "BTC"
    assert got.iloc[0]["max_supply"] == 21_000_000.0


def test_upsert_crypto_profile_overwrites_on_refresh(lake):
    """Supply moves daily; re-upserting the same ticker updates the row in
    place. (Supply *trajectory* is intentionally not preserved here — the
    spec keeps crypto_profiles a current-snapshot table.)"""
    initial = CryptoProfileRow(ticker="BTC-USD.CC", circulating_supply=19_700_000.0)
    later = CryptoProfileRow(ticker="BTC-USD.CC", circulating_supply=19_750_000.0)
    lake.upsert_crypto_profile(_rows_to_df([initial]))
    lake.upsert_crypto_profile(_rows_to_df([later]))
    assert lake.count_rows("crypto_profiles") == 1
    got = lake.sql("SELECT circulating_supply FROM crypto_profiles WHERE ticker = 'BTC-USD.CC'")
    assert got.iloc[0]["circulating_supply"] == 19_750_000.0


# ---- bond profiles + yield history ----------------------------------------


def test_upsert_bond_profile_round_trip(lake):
    row = BondProfileRow(
        ticker="US10Y.GBOND",
        issuer_name="United States Treasury",
        issuer_kind="sovereign",
        bond_kind="treasury",
        coupon_rate=4.25,
        coupon_frequency=2,
        face_value=1000.0,
        currency="USD",
        issue_date=date(2025, 11, 15),
        maturity_date=date(2035, 11, 15),
        credit_rating="AAA",
    )
    assert lake.upsert_bond_profile(_rows_to_df([row])) == 1
    got = lake.sql("SELECT * FROM bond_profiles WHERE ticker = 'US10Y.GBOND'")
    assert got.iloc[0]["issuer_kind"] == "sovereign"
    assert got.iloc[0]["coupon_rate"] == 4.25


def test_upsert_bond_yields_round_trip(lake):
    rows = [
        BondYieldRow(
            ticker="US10Y.GBOND", date=date(2026, 4, 1), yield_to_maturity=4.18, clean_price=99.45
        ),
        BondYieldRow(
            ticker="US10Y.GBOND", date=date(2026, 4, 2), yield_to_maturity=4.20, clean_price=99.40
        ),
    ]
    assert lake.upsert_bond_yields(_rows_to_df(rows)) == 2
    assert lake.count_rows("bond_yield_history") == 2


def test_upsert_bond_yields_is_idempotent(lake):
    row = BondYieldRow(
        ticker="US10Y.GBOND",
        date=date(2026, 4, 1),
        yield_to_maturity=4.18,
        clean_price=99.45,
    )
    lake.upsert_bond_yields(_rows_to_df([row]))
    # Same PK with a corrected yield → row updates in place, not duplicated.
    revised = BondYieldRow(
        ticker="US10Y.GBOND",
        date=date(2026, 4, 1),
        yield_to_maturity=4.19,
        clean_price=99.44,
    )
    lake.upsert_bond_yields(_rows_to_df([revised]))
    assert lake.count_rows("bond_yield_history") == 1
    got = lake.sql("SELECT yield_to_maturity FROM bond_yield_history WHERE ticker = 'US10Y.GBOND'")
    assert got.iloc[0]["yield_to_maturity"] == 4.19


# ---- commodity contracts ---------------------------------------------------


def test_upsert_commodity_contract_round_trip(lake):
    row = CommodityContractRow(
        ticker="GC.COMM",
        underlying_symbol="GC",
        contract_kind="continuous",
        contract_size=100.0,
        contract_unit="troy_ounce",
    )
    assert lake.upsert_commodity_contract(_rows_to_df([row])) == 1
    got = lake.sql("SELECT * FROM commodity_contracts WHERE ticker = 'GC.COMM'")
    assert got.iloc[0]["contract_kind"] == "continuous"
    assert got.iloc[0]["contract_unit"] == "troy_ounce"


# ---- empty inputs are no-ops ----------------------------------------------


def test_per_class_upserts_handle_empty_dataframes(lake):
    empty = pd.DataFrame()
    assert lake.upsert_crypto_profile(empty) == 0
    assert lake.upsert_bond_profile(empty) == 0
    assert lake.upsert_bond_yields(empty) == 0
    assert lake.upsert_commodity_contract(empty) == 0

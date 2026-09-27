"""Migration 038: the jurisdiction lives in ``portfolio_tax_settings`` and
the base currency in ``portfolios``. ``account_profiles`` loses both, and
existing rows are reconciled: the newer of the profile and the tax
settings wins."""

from __future__ import annotations

import pytest

from stonks.accounts.rules.profiles import get_profile
from stonks.store import state as state_module
from stonks.store.state import SqliteState

OLD = "2026-09-01T00:00:00+00:00"
NEW = "2026-09-20T00:00:00+00:00"


def _upto_037(path) -> SqliteState:
    old = SqliteState(path)
    old.con.execute(
        "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    for file in sorted(state_module.MIGRATIONS_DIR.glob("*.sql")):
        version = int(file.stem.split("_", 1)[0])
        if version >= 38:
            break
        old.con.executescript(
            f"BEGIN;\n{file.read_text(encoding='utf-8')}\n;\n"
            f"INSERT INTO schema_migrations VALUES ({version}, 'x');\nCOMMIT;"
        )
    return old


def _portfolio(state: SqliteState, pid: str) -> None:
    state.execute(
        "INSERT INTO portfolios (id, owner_id, name, kind, base_currency, created_at)"
        " VALUES (?, 'usr_owner', ?, 'simulated', 'USD', 'x')",
        [pid, pid],
    )


def _profile(state: SqliteState, pid: str, jurisdiction: str, base: str, at: str) -> None:
    state.execute(
        "INSERT INTO account_profiles (portfolio_id, jurisdiction, account_type, base_currency,"
        " wash_sale_mode, updated_at, updated_by) VALUES (?, ?, 'margin', ?, 'block', ?, 'u')",
        [pid, jurisdiction, base, at],
    )


def _tax(state: SqliteState, pid: str, jurisdiction: str, at: str) -> None:
    state.execute(
        "INSERT INTO portfolio_tax_settings (portfolio_id, jurisdiction, lot_method, wash_sales,"
        " updated_at, updated_by) VALUES (?, ?, 'specific', 0, ?, 't')",
        [pid, jurisdiction, at],
    )


@pytest.fixture
def migrated(tmp_path):
    old = _upto_037(tmp_path / "state.sqlite")
    for pid in ("pf_a", "pf_b", "pf_c"):
        _portfolio(old, pid)
    _profile(old, "pf_a", "uk", "GBP", NEW)  # no tax row: the profile wins
    _profile(old, "pf_b", "eu", "EUR", NEW)  # newer than the tax row: the profile wins
    _tax(old, "pf_b", "us", OLD)
    _profile(old, "pf_c", "eu", "EUR", OLD)  # older than the tax row: the tax row wins
    _tax(old, "pf_c", "uk", NEW)
    old.migrate()
    yield old
    old.close()


def _tax_row(state: SqliteState, pid: str):
    return state.sql("SELECT * FROM portfolio_tax_settings WHERE portfolio_id = ?", [pid])[0]


def _base(state: SqliteState, pid: str) -> str:
    return state.sql("SELECT base_currency FROM portfolios WHERE id = ?", [pid])[0][0]


def test_the_profile_loses_its_copies(migrated):
    cols = {r["name"] for r in migrated.sql("PRAGMA table_info(account_profiles)")}
    assert "jurisdiction" not in cols and "base_currency" not in cols
    assert {"account_type", "wash_sale_mode", "allow_short", "updated_by"} <= cols


def test_existing_rows_are_reconciled_newest_wins(migrated):
    assert _tax_row(migrated, "pf_a")["jurisdiction"] == "uk"
    assert _base(migrated, "pf_a") == "GBP"
    b = _tax_row(migrated, "pf_b")
    assert (b["jurisdiction"], b["lot_method"], b["wash_sales"]) == ("eu", "specific", 0)
    assert _base(migrated, "pf_b") == "EUR"
    assert _tax_row(migrated, "pf_c")["jurisdiction"] == "uk"
    assert _base(migrated, "pf_c") == "USD"


def test_profiles_keep_their_own_fields_and_read_the_rest_through(migrated):
    got = get_profile(migrated, "pf_b")
    assert got is not None
    assert (got.jurisdiction, got.base_currency, got.account_type) == ("eu", "EUR", "margin")
    assert got.wash_sale_mode == "block" and got.wash_sales is False

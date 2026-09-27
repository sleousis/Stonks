"""Account profiles per live portfolio (roadmap 19.7).

``account_profiles`` holds one row per portfolio. Setting it writes an
``audit_log`` row. Who may change it (the owner, with a fresh second
factor) is checked by the service layer before it calls
:func:`set_profile`. A cash profile never allows shorts (the table checks
it too).

Each fact has one home (migration 040). The jurisdiction and whether wash
sales apply live in ``portfolio_tax_settings``, the base currency in
``portfolios``. A profile reads them from there, and setting a profile
writes them there. ``account_profiles`` keeps only what is about the
broker account itself.
"""

from __future__ import annotations

from dataclasses import asdict

from stonks.accounts.audit import AuditLog
from stonks.accounts.rules import AccountProfile
from stonks.core.clock import SYSTEM_CLOCK, Clock, iso_now
from stonks.store.state import SqliteState

TABLE = "account_profiles"
_JURISDICTIONS = ("us", "eu", "uk")


class ProfileError(ValueError):
    """A profile that breaks a rule (a short on a cash account, ...)."""


def profiles_enabled(state: SqliteState) -> bool:
    rows = state.sql("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", [TABLE])
    return bool(rows)


def get_profile(state: SqliteState, portfolio_id: str) -> AccountProfile | None:
    if not profiles_enabled(state):
        return None
    rows = state.sql(
        f"SELECT a.*, COALESCE(t.jurisdiction, 'us') AS jurisdiction,"
        " COALESCE(t.wash_sales, 1) AS wash_sales,"
        " upper(COALESCE(p.base_currency, 'USD')) AS base_currency"
        f" FROM {TABLE} a"
        " LEFT JOIN portfolio_tax_settings t ON t.portfolio_id = a.portfolio_id"
        " LEFT JOIN portfolios p ON p.id = a.portfolio_id"
        " WHERE a.portfolio_id = ?",
        [portfolio_id],
    )
    if not rows:
        return None
    r = rows[0]
    return AccountProfile(
        portfolio_id=r["portfolio_id"],
        jurisdiction=r["jurisdiction"],
        account_type=r["account_type"],
        client_class=r["client_class"],
        base_currency=r["base_currency"],
        fx_policy=r["fx_policy"],
        wash_sale_mode=r["wash_sale_mode"],
        allow_short=bool(r["allow_short"]),
        wash_sales=bool(r["wash_sales"]),
    )


def validate_profile(profile: AccountProfile) -> None:
    if profile.jurisdiction not in _JURISDICTIONS:
        raise ProfileError(f"jurisdiction must be one of {_JURISDICTIONS}")
    if profile.allow_short and profile.account_type != "margin":
        raise ProfileError("shorts need a margin account")
    if len(profile.base_currency) != 3:
        raise ProfileError("base_currency must be a three-letter code")


def set_profile(
    state: SqliteState,
    profile: AccountProfile,
    *,
    actor: str,
    clock: Clock = SYSTEM_CLOCK,
) -> AccountProfile:
    """Insert or replace the portfolio's profile and audit the change. The
    jurisdiction goes to the tax settings and the base currency to the
    portfolio (their one home). ``wash_sales`` is read, never written here:
    it is a tax setting."""
    validate_profile(profile)
    now = iso_now(clock)
    with state.transaction():
        old = get_profile(state, profile.portfolio_id)
        state.execute(
            f"INSERT INTO {TABLE} (portfolio_id, account_type, client_class, fx_policy,"
            " wash_sale_mode, allow_short, updated_at, updated_by)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (portfolio_id) DO UPDATE SET account_type = excluded.account_type,"
            " client_class = excluded.client_class, fx_policy = excluded.fx_policy,"
            " wash_sale_mode = excluded.wash_sale_mode, allow_short = excluded.allow_short,"
            " updated_at = excluded.updated_at, updated_by = excluded.updated_by",
            [
                profile.portfolio_id,
                profile.account_type,
                profile.client_class,
                profile.fx_policy,
                profile.wash_sale_mode,
                int(profile.allow_short),
                now,
                actor,
            ],
        )
        state.execute(
            "INSERT INTO portfolio_tax_settings (portfolio_id, jurisdiction, updated_at,"
            " updated_by) VALUES (?, ?, ?, ?) ON CONFLICT (portfolio_id) DO UPDATE SET"
            " jurisdiction = excluded.jurisdiction, updated_at = excluded.updated_at,"
            " updated_by = excluded.updated_by",
            [profile.portfolio_id, profile.jurisdiction, now, actor],
        )
        state.execute(
            "UPDATE portfolios SET base_currency = ? WHERE id = ?",
            [profile.base_currency.upper(), profile.portfolio_id],
        )
        AuditLog(state).record(
            actor,
            "live.account_profile_set",
            "portfolio",
            profile.portfolio_id,
            portfolio_id=profile.portfolio_id,
            details={"profile": asdict(profile), "previous": None if old is None else asdict(old)},
        )
    got = get_profile(state, profile.portfolio_id)
    assert got is not None
    return got

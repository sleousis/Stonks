"""Paper trading for the auto gate (decision 2026-09-26): the paper-day
count and the paper accounts of broker portfolios.

**Paper days** come from ``portfolio_runs`` (written by the tick), the one
source for the auto gate and the subscriptions view: the distinct trading
days (``as_of``) of runs that traded the subscription in paper mode and
finished without an error or a risk breach, counted after its last breach
and after ``subscriptions.paper_since`` (set when it switched to notify).

**Paper accounts.** A paper subscription of a broker portfolio must never
reach the real account, yet it needs a book of its own to trade for the 20
days. It trades a simulated account: a portfolio row with ``paper_of`` set
to the broker portfolio, same owner, created by the tick on first use and
left out of portfolio lists.
"""

from __future__ import annotations

from dataclasses import replace

from stonks.accounts.audit import iso_now
from stonks.accounts.models import Portfolio
from stonks.store.state import SqliteState

#: Run statuses that count as a completed paper day.
_COUNTED = ("ok", "noop", "partial")


def paper_days_completed(state: SqliteState, subscription_id: str, since: str | None = None) -> int:
    """Completed paper days of ``subscription_id`` (see the module doc).
    ``since``: only runs started after this ISO timestamp count."""
    runs = (
        "SELECT r.as_of, r.status, r.risk_breached, r.started_at"
        " FROM portfolio_runs r, json_each(r.paper_subscriptions_json) j WHERE j.value = ?"
    )
    params: list[object] = [subscription_id]
    if since is not None:
        runs += " AND r.started_at > ?"
        params.append(since)
    marks = ", ".join("?" for _ in _COUNTED)
    row = state.sql(
        f"WITH runs AS ({runs}),"
        " breach AS (SELECT MAX(as_of) AS as_of FROM runs WHERE risk_breached = 1)"
        " SELECT COUNT(DISTINCT as_of) AS n FROM runs"
        f" WHERE risk_breached = 0 AND status IN ({marks})"
        " AND as_of > COALESCE((SELECT as_of FROM breach), '')",
        [*params, *_COUNTED],
    )[0]
    return int(row["n"])


def paper_account_id(portfolio_id: str) -> str:
    return f"{portfolio_id}_paper"


def ensure_paper_account(
    state: SqliteState, portfolio: Portfolio, *, create: bool = True
) -> Portfolio:
    """The simulated paper account of broker portfolio ``portfolio``,
    created on first use with the same owner, cash, universe, risk policy
    and construction. Call inside or outside a transaction. ``create=False``
    (a dry run) writes nothing: a missing account is returned as it would be
    created, without its row (BE-52)."""
    account_id = paper_account_id(portfolio.id)
    rows = state.sql("SELECT * FROM portfolios WHERE id = ?", [account_id])
    if not rows and not create:
        return replace(
            portfolio,
            id=account_id,
            name=f"{portfolio.name} (paper)",
            kind="simulated",
            broker_connection_id=None,
            external_account_id=None,
        )
    if not rows:
        name = f"{portfolio.name} (paper)"
        taken = state.sql(
            "SELECT 1 FROM portfolios WHERE owner_id = ? AND name = ?", [portfolio.owner_id, name]
        )
        if taken:
            name = f"{portfolio.name} (paper {portfolio.id})"
        state.execute(
            "INSERT INTO portfolios (id, owner_id, name, kind, base_currency, initial_cash,"
            " allow_short, universe, risk_policy_json, construction_json, status, created_at,"
            " paper_of) SELECT ?, owner_id, ?, 'simulated', base_currency, initial_cash,"
            " allow_short, universe, risk_policy_json, construction_json, 'active', ?, id"
            " FROM portfolios WHERE id = ?",
            [account_id, name, iso_now(), portfolio.id],
        )
        rows = state.sql("SELECT * FROM portfolios WHERE id = ?", [account_id])
    return Portfolio.from_row(rows[0])

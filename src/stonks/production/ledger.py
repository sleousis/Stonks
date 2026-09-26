"""Portfolio scoping of the ledger tables (``orders``, ``fills``,
``portfolio_snapshots``): the one place readers get their WHERE clause.

Every reader of those tables filters by portfolio (design
``docs/design/accounts-and-modes.md`` section 3: repository functions for
scoped tables take ``portfolio_id``; there is no unscoped variant outside
admin services). Readers default to the default portfolio, so single-owner
installs and existing callers read exactly what they read before.

``tick_only`` also drops the snapshots a broker sync wrote
(``source = 'sync'``): the tick resumes its own ledger, never a sync's copy
of the account. A database on an older schema (no ``portfolio_id`` or
``source`` column yet) holds one book, so the missing filter is skipped.
"""

from __future__ import annotations

from typing import Any, Literal

from stonks.accounts.models import DEFAULT_PORTFOLIO_ID
from stonks.store.state import SqliteState

LedgerTable = Literal["orders", "fills", "portfolio_snapshots"]


def ledger_columns(state: SqliteState, table: LedgerTable) -> frozenset[str]:
    return frozenset(r["name"] for r in state.sql(f"SELECT name FROM pragma_table_info('{table}')"))


def ledger_filter(
    state: SqliteState,
    table: LedgerTable,
    portfolio_id: str | None = DEFAULT_PORTFOLIO_ID,
    *,
    alias: str | None = None,
    tick_only: bool = False,
) -> tuple[str, list[Any]]:
    """A predicate (``"1 = 1"`` when nothing narrows) and its parameters
    that keep ``table``'s rows of ``portfolio_id`` (``None``: every
    portfolio, admin reads only) and, with ``tick_only``, only rows the
    tick wrote. ``alias`` prefixes the columns (``"o"`` -> ``o.portfolio_id``)."""
    cols = ledger_columns(state, table)
    prefix = f"{alias}." if alias else ""
    clauses: list[str] = []
    params: list[Any] = []
    if portfolio_id is not None and "portfolio_id" in cols:
        clauses.append(f"{prefix}portfolio_id = ?")
        params.append(portfolio_id)
    if tick_only and "source" in cols:
        clauses.append(f"{prefix}source = 'tick'")
    return (" AND ".join(clauses) or "1 = 1"), params

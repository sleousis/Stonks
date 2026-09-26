"""Append-only ``audit_log``: one row per user action (mode changes,
portfolio changes, user admin, cross-user reads). Strategy lifecycle stays in
``status_changes`` (``StrategyRegistry``). Triggers make the table
append-only; callers write the row in the same transaction as the change."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from stonks.accounts.models import AccountsError, AuditEntry
from stonks.accounts.scope import Scope, owned_portfolio
from stonks.store.state import SqliteState


class AuditLog:
    def __init__(self, state: SqliteState) -> None:
        self._state = state

    def record(
        self,
        actor: str,
        action: str,
        target_kind: str,
        target_id: str | None = None,
        *,
        portfolio_id: str | None = None,
        details: Mapping[str, Any] | None = None,
        ip: str | None = None,
    ) -> int:
        if not isinstance(actor, str) or not actor.strip():
            raise AccountsError("an actor is required for every audited action")
        cur = self._state.execute(
            "INSERT INTO audit_log (actor, action, target_kind, target_id, portfolio_id,"
            " details_json, ip, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                actor.strip(),
                action,
                target_kind,
                target_id,
                portfolio_id,
                json.dumps(dict(details or {}), sort_keys=True, default=str),
                ip,
                iso_now(),
            ],
        )
        return int(cur.lastrowid or 0)

    def for_portfolio(self, scope: Scope, portfolio_id: str) -> list[AuditEntry]:
        owned_portfolio(self._state, scope, portfolio_id)
        rows = self._state.sql(
            "SELECT * FROM audit_log WHERE portfolio_id = ? ORDER BY id", [portfolio_id]
        )
        return [AuditEntry.from_row(r) for r in rows]

    def by_actor(self, scope: Scope) -> list[AuditEntry]:
        """The principal's own actions."""
        rows = self._state.sql("SELECT * FROM audit_log WHERE actor = ? ORDER BY id", [scope.actor])
        return [AuditEntry.from_row(r) for r in rows]


def iso_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")

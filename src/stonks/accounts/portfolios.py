"""``portfolios`` repository. Every call takes a :class:`Scope`; reads and
writes of a portfolio go through :func:`owned_portfolio`, so another user's
portfolio is "not found". Risk and construction overrides are stored as
partial mappings and merged tighten-only by :mod:`stonks.accounts.book`."""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from stonks.accounts.audit import AuditLog, iso_now
from stonks.accounts.book import partial_risk_policy
from stonks.accounts.models import AccountsError, Portfolio, PortfolioKind, PortfolioStatus
from stonks.accounts.scope import Scope, owned_portfolio
from stonks.store.state import SqliteState


class PortfolioRepository:
    def __init__(self, state: SqliteState) -> None:
        self._state = state
        self._audit = AuditLog(state)

    def get(self, scope: Scope, portfolio_id: str) -> Portfolio:
        return owned_portfolio(self._state, scope, portfolio_id)

    def list(self, scope: Scope) -> list[Portfolio]:
        """The principal's own portfolios (a service scope: all of them)."""
        if scope.is_service:
            rows = self._state.sql("SELECT * FROM portfolios ORDER BY created_at, id")
        else:
            rows = self._state.sql(
                "SELECT p.* FROM portfolios p JOIN users u ON u.id = p.owner_id"
                " WHERE p.owner_id = ? AND u.status = 'active' ORDER BY p.created_at, p.id",
                [scope.user_id],
            )
        return [Portfolio.from_row(r) for r in rows]

    def create(
        self,
        scope: Scope,
        *,
        name: str,
        kind: PortfolioKind = "simulated",
        base_currency: str = "USD",
        initial_cash: float | None = None,
        allow_short: bool = False,
        universe: Sequence[str] | None = None,
        risk_policy: Mapping[str, Any] | None = None,
        construction: Mapping[str, Any] | None = None,
    ) -> Portfolio:
        if scope.is_service:
            raise AccountsError("portfolios are owned by people, not services")
        portfolio_id = f"pf_{uuid.uuid4().hex[:12]}"
        with self._state.transaction():
            self._state.execute(
                "INSERT INTO portfolios (id, owner_id, name, kind, base_currency, initial_cash,"
                " allow_short, universe, risk_policy_json, construction_json, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    portfolio_id,
                    scope.user_id,
                    name,
                    kind,
                    base_currency,
                    initial_cash,
                    int(bool(allow_short)),
                    json.dumps(list(universe)) if universe is not None else None,
                    json.dumps(partial_risk_policy(risk_policy), sort_keys=True),
                    json.dumps(dict(construction or {}), sort_keys=True),
                    iso_now(),
                ],
            )
            self._audit.record(
                scope.actor,
                "portfolio.create",
                "portfolio",
                portfolio_id,
                portfolio_id=portfolio_id,
                details={"name": name, "kind": kind},
            )
        return self.get(scope, portfolio_id)

    def set_risk_policy(
        self, scope: Scope, portfolio_id: str, overrides: Mapping[str, Any]
    ) -> Portfolio:
        before = self.get(scope, portfolio_id)
        stored = partial_risk_policy(overrides)
        with self._state.transaction():
            self._state.execute(
                "UPDATE portfolios SET risk_policy_json = ? WHERE id = ?",
                [json.dumps(stored, sort_keys=True), portfolio_id],
            )
            self._audit.record(
                scope.actor,
                "portfolio.risk_policy",
                "portfolio",
                portfolio_id,
                portfolio_id=portfolio_id,
                details={"from": before.risk_policy, "to": stored},
            )
        return self.get(scope, portfolio_id)

    def set_status(self, scope: Scope, portfolio_id: str, status: PortfolioStatus) -> Portfolio:
        before = self.get(scope, portfolio_id)
        with self._state.transaction():
            self._state.execute(
                "UPDATE portfolios SET status = ? WHERE id = ?", [status, portfolio_id]
            )
            self._audit.record(
                scope.actor,
                "portfolio.status",
                "portfolio",
                portfolio_id,
                portfolio_id=portfolio_id,
                details={"from": before.status, "to": status},
            )
        return self.get(scope, portfolio_id)

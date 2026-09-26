"""``subscriptions`` repository: which strategies a user follows, in which
mode, for which portfolio.

Mode rules (design section 4, decision 2026-09-26):

- ``notify`` needs no portfolio; ``paper`` and ``auto`` need one the user owns.
- Retired strategies take no new subscriptions; shadow ones allow notify and
  paper only.
- A subscription is never created in ``auto``. Switching to auto passes
  :meth:`SubscriptionRepository.auto_blockers`: the strategy is active, the
  portfolio is a broker portfolio and active, and the subscription completed
  ``MIN_PAPER_DAYS_FOR_AUTO`` trading days in paper without a risk breach.
  Checks owned by later steps (2FA step-up, connection health, kill switch)
  are added by their services on top.
- The paper-day count only grows while in paper (the tick records each day
  through :meth:`record_paper_day`); a breach or a switch to notify restarts
  it; auto back to paper keeps it.
- Every mode change writes an ``audit_log`` row in the same transaction.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from datetime import date
from typing import Any

from stonks.accounts.audit import AuditLog, iso_now
from stonks.accounts.book import partial_risk_policy
from stonks.accounts.models import (
    MIN_PAPER_DAYS_FOR_AUTO,
    AccountsError,
    AutoGateRefused,
    Mode,
    NotFound,
    Portfolio,
    Subscription,
)
from stonks.accounts.scope import Scope, owned_portfolio
from stonks.store.state import SqliteState

#: Modes a strategy's status allows for a subscription (auto additionally
#: needs the auto gate).
_ALLOWED_MODES: dict[str, frozenset[Mode]] = {
    "shadow": frozenset({Mode.NOTIFY, Mode.PAPER}),
    "active": frozenset({Mode.NOTIFY, Mode.PAPER, Mode.AUTO}),
    "retired": frozenset(),
}


class SubscriptionRepository:
    def __init__(self, state: SqliteState) -> None:
        self._state = state
        self._audit = AuditLog(state)

    # ---- reads -------------------------------------------------------------

    def get(self, scope: Scope, subscription_id: str) -> Subscription:
        if scope.is_service:
            rows = self._state.sql("SELECT * FROM subscriptions WHERE id = ?", [subscription_id])
        else:
            rows = self._state.sql(
                "SELECT s.* FROM subscriptions s JOIN users u ON u.id = s.user_id"
                " WHERE s.id = ? AND s.user_id = ? AND u.status = 'active'",
                [subscription_id, scope.user_id],
            )
        if not rows:
            raise NotFound(f"subscription {subscription_id!r} not found")
        return Subscription.from_row(rows[0])

    def list_for_user(self, scope: Scope) -> list[Subscription]:
        rows = self._state.sql(
            "SELECT s.* FROM subscriptions s JOIN users u ON u.id = s.user_id"
            " WHERE s.user_id = ? AND u.status = 'active' ORDER BY s.created_at, s.id",
            [scope.user_id],
        )
        return [Subscription.from_row(r) for r in rows]

    def list_for_portfolio(self, scope: Scope, portfolio_id: str) -> list[Subscription]:
        owned_portfolio(self._state, scope, portfolio_id)
        rows = self._state.sql(
            "SELECT * FROM subscriptions WHERE portfolio_id = ? ORDER BY created_at, id",
            [portfolio_id],
        )
        return [Subscription.from_row(r) for r in rows]

    # ---- writes ------------------------------------------------------------

    def subscribe(
        self,
        scope: Scope,
        *,
        strategy_id: str,
        mode: Mode = Mode.NOTIFY,
        portfolio_id: str | None = None,
        weight: float = 1.0,
        risk_overrides: Mapping[str, Any] | None = None,
    ) -> Subscription:
        mode = Mode(mode)
        if scope.is_service:
            raise AccountsError("subscriptions belong to people, not services")
        if mode is Mode.AUTO:
            raise AutoGateRefused(["start in paper; auto needs a paper track record first"])
        status = self._strategy_status(strategy_id)
        self._check_mode_allowed(status, mode, strategy_id)
        if portfolio_id is not None:
            owned_portfolio(self._state, scope, portfolio_id)
        elif mode.needs_portfolio:
            raise AccountsError(f"{mode.value} mode needs a portfolio")
        if not weight >= 0:
            raise AccountsError(f"weight must be >= 0, got {weight}")
        sub_id = f"sub_{uuid.uuid4().hex[:12]}"
        now = iso_now()
        with self._state.transaction():
            self._state.execute(
                "INSERT INTO subscriptions (id, user_id, strategy_id, portfolio_id, mode, weight,"
                " risk_overrides_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    sub_id,
                    scope.user_id,
                    strategy_id,
                    portfolio_id,
                    mode.value,
                    float(weight),
                    json.dumps(partial_risk_policy(risk_overrides), sort_keys=True),
                    now,
                    now,
                ],
            )
            self._audit.record(
                scope.actor,
                "subscription.create",
                "subscription",
                sub_id,
                portfolio_id=portfolio_id,
                details={"strategy_id": strategy_id, "mode": mode.value},
            )
        return self.get(scope, sub_id)

    def auto_blockers(self, scope: Scope, subscription_id: str) -> list[str]:
        """Every data-model check of the auto checklist that fails (empty =
        pass). Shown in the UI checklist; enforced by :meth:`set_mode`."""
        sub = self.get(scope, subscription_id)
        return self._auto_blockers(scope, sub)

    def set_mode(
        self, scope: Scope, subscription_id: str, mode: Mode, *, reason: str | None = None
    ) -> Subscription:
        mode = Mode(mode)
        sub = self.get(scope, subscription_id)
        if mode is sub.mode and not sub.auto_paused:
            return sub
        if mode is Mode.AUTO:
            # The gate reports every failure at once (strategy status included).
            blockers = self._auto_blockers(scope, sub)
            if blockers:
                raise AutoGateRefused(blockers)
        else:
            self._check_mode_allowed(self._strategy_status(sub.strategy_id), mode, sub.strategy_id)
            if mode.needs_portfolio and sub.portfolio_id is None:
                raise AccountsError(f"{mode.value} mode needs a portfolio")

        sets: dict[str, Any] = {"mode": mode.value, "updated_at": iso_now()}
        if mode is Mode.AUTO:
            sets |= {
                "auto_enabled_at": sets["updated_at"],
                "auto_enabled_by": scope.actor,
                "paused_reason": None,
            }
        else:
            sets |= {"auto_enabled_at": None, "auto_enabled_by": None, "paused_reason": None}
        if mode is Mode.NOTIFY:
            sets |= {"paper_days_completed": 0, "paper_last_as_of": None}
        assignments = ", ".join(f"{col} = ?" for col in sets)
        with self._state.transaction():
            self._state.execute(
                f"UPDATE subscriptions SET {assignments} WHERE id = ?",
                [*sets.values(), sub.id],
            )
            details: dict[str, Any] = {"from": sub.mode.value, "to": mode.value}
            if reason:
                details["reason"] = reason
            self._audit.record(
                scope.actor,
                "subscription.mode",
                "subscription",
                sub.id,
                portfolio_id=sub.portfolio_id,
                details=details,
            )
        return self.get(scope, sub.id)

    def enable(
        self, scope: Scope, subscription_id: str, *, reason: str | None = None
    ) -> Subscription:
        """Turn a subscription back on. Idempotent."""
        return self._set_enabled(scope, subscription_id, True, reason)

    def disable(
        self, scope: Scope, subscription_id: str, *, reason: str | None = None
    ) -> Subscription:
        """Turn a subscription off: its strategy places no orders and sends
        no signals for it until enabled again. The mode and the paper-day
        count are kept. Idempotent."""
        return self._set_enabled(scope, subscription_id, False, reason)

    def _set_enabled(
        self, scope: Scope, subscription_id: str, enabled: bool, reason: str | None
    ) -> Subscription:
        sub = self.get(scope, subscription_id)
        if sub.enabled is enabled:
            return sub
        with self._state.transaction():
            self._state.execute(
                "UPDATE subscriptions SET enabled = ?, updated_at = ? WHERE id = ?",
                [int(enabled), iso_now(), sub.id],
            )
            self._audit.record(
                scope.actor,
                "subscription.enable" if enabled else "subscription.disable",
                "subscription",
                sub.id,
                portfolio_id=sub.portfolio_id,
                details={"reason": reason} if reason else {},
            )
        return self.get(scope, sub.id)

    def record_paper_day(
        self, scope: Scope, subscription_id: str, as_of: date, *, breached: bool
    ) -> Subscription:
        """One trading day of paper mode, recorded by the tick (a service).
        Idempotent per ``as_of``: a re-run or an older day changes nothing.
        ``breached`` (the slice broke a risk limit that day) restarts the count."""
        if not scope.is_service:
            raise AccountsError("paper days are recorded by the tick, not by users")
        sub = self.get(scope, subscription_id)
        day = as_of.isoformat()
        if sub.mode is not Mode.PAPER or not sub.enabled:
            return sub
        if sub.paper_last_as_of is not None and day <= sub.paper_last_as_of:
            return sub
        days = 0 if breached else sub.paper_days_completed + 1
        self._state.execute(
            "UPDATE subscriptions SET paper_days_completed = ?, paper_last_as_of = ?,"
            " updated_at = ? WHERE id = ?",
            [days, day, iso_now(), sub.id],
        )
        return self.get(scope, sub.id)

    # ---- helpers -----------------------------------------------------------

    def _strategy_status(self, strategy_id: str) -> str:
        rows = self._state.sql("SELECT status FROM strategies WHERE id = ?", [strategy_id])
        if not rows:
            raise NotFound(f"strategy {strategy_id!r} not found")
        return str(rows[0]["status"])

    @staticmethod
    def _check_mode_allowed(status: str, mode: Mode, strategy_id: str) -> None:
        if mode not in _ALLOWED_MODES.get(status, frozenset()):
            raise AccountsError(
                f"strategy {strategy_id!r} is {status}; {mode.value} mode is not allowed"
            )

    def _auto_blockers(self, scope: Scope, sub: Subscription) -> list[str]:
        reasons: list[str] = []
        if self._strategy_status(sub.strategy_id) != "active":
            reasons.append("the strategy is not active")
        portfolio: Portfolio | None = None
        if sub.portfolio_id is None:
            reasons.append("auto mode needs a portfolio")
        else:
            portfolio = owned_portfolio(self._state, scope, sub.portfolio_id)
            if portfolio.kind != "broker":
                reasons.append("auto mode needs a broker portfolio")
            if portfolio.status != "active":
                reasons.append(f"the portfolio is {portfolio.status}")
        if sub.paper_days_completed < MIN_PAPER_DAYS_FOR_AUTO:
            reasons.append(
                f"{sub.paper_days_completed} of {MIN_PAPER_DAYS_FOR_AUTO} paper trading days"
                " completed without a risk breach"
            )
        return reasons

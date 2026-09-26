"""Typed rows and vocabularies of the accounts data model (roadmap 15.2).

See ``docs/design/accounts-and-modes.md`` and migration ``010_accounts.sql``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal

#: The admin and portfolio every pre-accounts install is migrated to.
DEFAULT_OWNER_ID = "usr_owner"
DEFAULT_PORTFOLIO_ID = "pf_default"

#: Trading days a subscription must complete in paper mode, without breaking
#: its risk limits, before it may switch to auto (decision 2026-09-26).
MIN_PAPER_DAYS_FOR_AUTO = 20

UserKind = Literal["human", "service"]
UserStatus = Literal["active", "disabled"]
PortfolioKind = Literal["simulated", "broker"]
PortfolioStatus = Literal["active", "paused", "archived"]


class Role(StrEnum):
    VIEWER = "viewer"
    TRADER = "trader"
    ADMIN = "admin"

    @property
    def can_trade(self) -> bool:
        return self in (Role.TRADER, Role.ADMIN)

    @property
    def is_admin(self) -> bool:
        return self is Role.ADMIN


class Mode(StrEnum):
    """How a subscription acts on its strategy's signals."""

    NOTIFY = "notify"
    PAPER = "paper"
    AUTO = "auto"

    @property
    def needs_portfolio(self) -> bool:
        return self is not Mode.NOTIFY

    @property
    def places_orders(self) -> bool:
        return self is not Mode.NOTIFY


class AccountsError(ValueError):
    """An accounts rule was broken (bad mode change, bad input, ...)."""


class NotFound(LookupError):
    """The row doesn't exist *or* the principal may not see it. Both read the
    same, so ids never leak (HTTP 404, never 403)."""


class AutoGateRefused(AccountsError):
    """A switch to auto failed the checklist; ``reasons`` lists each failure."""

    def __init__(self, reasons: list[str]) -> None:
        super().__init__("auto mode refused: " + "; ".join(reasons))
        self.reasons = list(reasons)


@dataclass(frozen=True)
class User:
    id: str
    kind: UserKind
    email: str | None
    display_name: str
    role: Role
    status: UserStatus
    timezone: str
    created_at: str
    last_login_at: str | None

    @classmethod
    def from_row(cls, row: Any) -> User:
        return cls(
            id=row["id"],
            kind=row["kind"],
            email=row["email"],
            display_name=row["display_name"],
            role=Role(row["role"]),
            status=row["status"],
            timezone=row["timezone"],
            created_at=row["created_at"],
            last_login_at=row["last_login_at"],
        )


@dataclass(frozen=True)
class Portfolio:
    """A book of money owned by one user. Named ``Portfolio`` in the accounts
    namespace; the positions-and-cash value object stays ``core.types.Portfolio``."""

    id: str
    owner_id: str
    name: str
    kind: PortfolioKind
    broker_connection_id: str | None
    external_account_id: str | None
    base_currency: str
    initial_cash: float | None
    allow_short: bool
    universe: tuple[str, ...] | None
    risk_policy: dict[str, Any]
    construction: dict[str, Any]
    status: PortfolioStatus
    created_at: str

    @classmethod
    def from_row(cls, row: Any) -> Portfolio:
        universe = json.loads(row["universe"]) if row["universe"] is not None else None
        return cls(
            id=row["id"],
            owner_id=row["owner_id"],
            name=row["name"],
            kind=row["kind"],
            broker_connection_id=row["broker_connection_id"],
            external_account_id=row["external_account_id"],
            base_currency=row["base_currency"],
            initial_cash=row["initial_cash"],
            allow_short=bool(row["allow_short"]),
            universe=tuple(universe) if universe is not None else None,
            risk_policy=json.loads(row["risk_policy_json"] or "{}"),
            construction=json.loads(row["construction_json"] or "{}"),
            status=row["status"],
            created_at=row["created_at"],
        )


@dataclass(frozen=True)
class Subscription:
    id: str
    user_id: str
    strategy_id: str
    portfolio_id: str | None
    mode: Mode
    weight: float
    risk_overrides: dict[str, Any]
    enabled: bool
    paper_days_completed: int
    paper_last_as_of: str | None
    auto_enabled_at: str | None
    auto_enabled_by: str | None
    paused_reason: str | None
    created_at: str
    updated_at: str

    @property
    def auto_paused(self) -> bool:
        return self.mode is Mode.AUTO and self.paused_reason is not None

    @classmethod
    def from_row(cls, row: Any) -> Subscription:
        return cls(
            id=row["id"],
            user_id=row["user_id"],
            strategy_id=row["strategy_id"],
            portfolio_id=row["portfolio_id"],
            mode=Mode(row["mode"]),
            weight=float(row["weight"]),
            risk_overrides=json.loads(row["risk_overrides_json"] or "{}"),
            enabled=bool(row["enabled"]),
            paper_days_completed=int(row["paper_days_completed"]),
            paper_last_as_of=row["paper_last_as_of"],
            auto_enabled_at=row["auto_enabled_at"],
            auto_enabled_by=row["auto_enabled_by"],
            paused_reason=row["paused_reason"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


@dataclass(frozen=True)
class AuditEntry:
    id: int
    actor: str
    action: str
    target_kind: str
    target_id: str | None
    portfolio_id: str | None
    details: dict[str, Any]
    ip: str | None
    created_at: str

    @classmethod
    def from_row(cls, row: Any) -> AuditEntry:
        return cls(
            id=int(row["id"]),
            actor=row["actor"],
            action=row["action"],
            target_kind=row["target_kind"],
            target_id=row["target_id"],
            portfolio_id=row["portfolio_id"],
            details=json.loads(row["details_json"] or "{}"),
            ip=row["ip"],
            created_at=row["created_at"],
        )

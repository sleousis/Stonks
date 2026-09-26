"""Who may do what, in one place.

Each :class:`Permission` maps to a :class:`Rule`: the roles that may hold
it, the credential scopes that grant it, and whether it needs a fresh
second factor (step-up) or a browser session. Services call
:func:`require`; routers never check roles themselves.

Step-up actions are refused for API tokens (403 ``step_up_required``):
they are done in the UI, where the user can prove the second factor.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from stonks.accounts import Role
from stonks.auth.errors import PermissionDenied, StepUpRequired
from stonks.auth.principal import ApiScope, Principal

_ALL = frozenset(Role)
_TRADERS = frozenset({Role.TRADER, Role.ADMIN})
_ADMINS = frozenset({Role.ADMIN})


class Permission(StrEnum):
    READ = "data.read"
    PORTFOLIO_MANAGE = "portfolio.manage"
    PORTFOLIO_TRADE = "portfolio.trade"
    AUTO_ENABLE = "subscription.auto_enable"
    CONNECTION_MANAGE = "connection.manage"
    LAB_RUN = "lab.run"
    KILLSWITCH_USER = "killswitch.user"
    KILLSWITCH_RESUME = "killswitch.resume"
    RISK_RESET = "risk.reset"
    NOTIFICATIONS_MANAGE = "notifications.manage"
    KILLSWITCH_GLOBAL = "killswitch.global"
    STRATEGY_PROMOTE = "strategy.promote"
    CODE_STRATEGIES = "strategy.code"
    OPERATIONS_RUN = "operations.run"
    RISK_GLOBAL = "risk.global"
    PORTFOLIO_TOTALS = "portfolio.totals"
    USERS_READ = "users.read"
    USERS_MANAGE = "users.manage"
    TOKENS_MANAGE = "tokens.manage"
    TOKENS_REVOKE = "tokens.revoke"
    RECOVERY_CODES = "mfa.recovery_codes"
    PASSWORD_CHANGE = "password.change"


@dataclass(frozen=True)
class Rule:
    roles: frozenset[Role]
    scopes: frozenset[ApiScope]  # any one of them grants it
    step_up: bool = False
    session_only: bool = False


POLICY: dict[Permission, Rule] = {
    Permission.READ: Rule(_ALL, frozenset({ApiScope.READ})),
    Permission.PORTFOLIO_MANAGE: Rule(_TRADERS, frozenset({ApiScope.TRADE})),
    Permission.PORTFOLIO_TRADE: Rule(_TRADERS, frozenset({ApiScope.TRADE})),
    Permission.AUTO_ENABLE: Rule(_TRADERS, frozenset({ApiScope.TRADE}), step_up=True),
    Permission.CONNECTION_MANAGE: Rule(_TRADERS, frozenset({ApiScope.TRADE}), step_up=True),
    Permission.LAB_RUN: Rule(_TRADERS, frozenset({ApiScope.LAB})),
    Permission.KILLSWITCH_USER: Rule(_TRADERS, frozenset({ApiScope.TRADE})),
    Permission.KILLSWITCH_RESUME: Rule(_TRADERS, frozenset({ApiScope.TRADE}), step_up=True),
    Permission.RISK_RESET: Rule(_TRADERS, frozenset({ApiScope.TRADE})),
    Permission.NOTIFICATIONS_MANAGE: Rule(_TRADERS, frozenset({ApiScope.TRADE})),
    Permission.KILLSWITCH_GLOBAL: Rule(_ADMINS, frozenset({ApiScope.ADMIN})),
    Permission.STRATEGY_PROMOTE: Rule(_ADMINS, frozenset({ApiScope.ADMIN})),
    Permission.CODE_STRATEGIES: Rule(_ADMINS, frozenset({ApiScope.ADMIN})),
    Permission.OPERATIONS_RUN: Rule(_ADMINS, frozenset({ApiScope.ADMIN})),
    Permission.RISK_GLOBAL: Rule(_ADMINS, frozenset({ApiScope.ADMIN})),
    # Sums across every trader, never anyone's holdings.
    Permission.PORTFOLIO_TOTALS: Rule(_ADMINS, frozenset({ApiScope.ADMIN})),
    Permission.USERS_READ: Rule(_ADMINS, frozenset({ApiScope.ADMIN})),
    Permission.USERS_MANAGE: Rule(_ADMINS, frozenset({ApiScope.ADMIN}), step_up=True),
    Permission.TOKENS_MANAGE: Rule(_ALL, frozenset({ApiScope.READ}), session_only=True),
    # Any credential of the owner may revoke (a leaked token can revoke itself).
    Permission.TOKENS_REVOKE: Rule(_ALL, frozenset(ApiScope)),
    Permission.RECOVERY_CODES: Rule(_ALL, frozenset({ApiScope.READ}), step_up=True),
    Permission.PASSWORD_CHANGE: Rule(_ALL, frozenset({ApiScope.READ}), step_up=True),
}


def allowed(principal: Principal, permission: Permission) -> bool:
    try:
        require(principal, permission)
    except PermissionDenied:
        return False
    return True


def require(principal: Principal, permission: Permission) -> None:
    """Raise :class:`PermissionDenied` (or :class:`StepUpRequired`) unless
    ``principal`` holds ``permission``."""
    rule = POLICY[permission]
    if principal.role not in rule.roles or not (principal.scopes & rule.scopes):
        raise PermissionDenied(f"{permission.value} is not allowed for this principal")
    if rule.session_only and principal.via != "session":
        raise PermissionDenied(f"{permission.value} needs a signed-in browser session")
    if rule.step_up and not (principal.via == "session" and principal.mfa_fresh):
        raise StepUpRequired(
            f"{permission.value} needs a fresh second factor; confirm it in the web app"
        )

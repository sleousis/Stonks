"""Accounts: users, portfolios, subscriptions and the audit log (roadmap 15.2).

Market facts and the strategy catalog are global; money, people and
delivery are scoped. Every read of a portfolio-scoped row goes through a
:class:`~stonks.accounts.scope.Scope` and :func:`~stonks.accounts.scope.owned_portfolio`,
which answers "not found" for rows the principal doesn't own. See
``docs/design/accounts-and-modes.md``.
"""

from stonks.accounts.audit import AuditLog
from stonks.accounts.book import BookSpec, merge_construction, tighter_of
from stonks.accounts.models import (
    DEFAULT_OWNER_ID,
    DEFAULT_PORTFOLIO_ID,
    MAX_WEIGHT,
    MIN_PAPER_DAYS_FOR_AUTO,
    AccountsError,
    AuditEntry,
    AutoGateRefused,
    Mode,
    NotFound,
    Portfolio,
    Role,
    Subscription,
    User,
)
from stonks.accounts.portfolios import PortfolioRepository
from stonks.accounts.scope import Scope, owned_portfolio
from stonks.accounts.subscriptions import SubscriptionRepository
from stonks.accounts.users import UserRepository

__all__ = [
    "DEFAULT_OWNER_ID",
    "DEFAULT_PORTFOLIO_ID",
    "MAX_WEIGHT",
    "MIN_PAPER_DAYS_FOR_AUTO",
    "AccountsError",
    "AuditEntry",
    "AuditLog",
    "AutoGateRefused",
    "BookSpec",
    "Mode",
    "NotFound",
    "Portfolio",
    "PortfolioRepository",
    "Role",
    "Scope",
    "Subscription",
    "SubscriptionRepository",
    "User",
    "UserRepository",
    "merge_construction",
    "owned_portfolio",
    "tighter_of",
]

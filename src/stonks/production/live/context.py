"""What the live safeguards see of a book at a real broker (roadmap 19.6).

:class:`LiveContext` rides on ``RiskContext.live``. It is ``None`` in
backtests and paper books, and every live rule then does nothing, so
backtests stay identical.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from stonks.execution.brokers.base import LiveAccountState, Quote

if TYPE_CHECKING:
    from stonks.accounts.rules import AccountRuleInputs

#: ``broker_paper``: the broker's paper account. ``live``: real money. The
#: safeguards run in both, so the paper soak exercises them.
LiveStage = Literal["broker_paper", "live"]


@dataclass(frozen=True)
class LiveContext:
    portfolio_id: str
    #: The amount the owner lets Stonks trade in this portfolio, in the
    #: account's base currency. ``None``: no allocation set, nothing opens.
    allocation: float | None = None
    stage: LiveStage = "live"
    #: The account as the broker reports it (``None`` when it could not be
    #: read: rules that need it refuse opening orders).
    account: LiveAccountState | None = None
    #: Snapshots for the tickers of the run's orders.
    quotes: Mapping[str, Quote] = field(default_factory=dict[str, Quote])
    #: Notional of the opening orders already sent today: by this
    #: portfolio, by every portfolio of its owner, and by everyone.
    sent_today: float = 0.0
    sent_today_user: float = 0.0
    sent_today_global: float = 0.0
    #: The owner's own positions in a shared account (never traded, never
    #: counted as the book's, BE-02).
    external_positions: Mapping[str, float] = field(default_factory=dict[str, float])
    #: The account rules engine's inputs (profile, settlement ledger,
    #: day trades, ...). ``None``: the ``account_rules`` rule refuses opens.
    account_rules: AccountRuleInputs | None = None

    def room_left(self, cap: float | None, sent: float) -> float | None:
        """What is left of a daily ``cap`` after ``sent`` (``None``: no cap)."""
        return None if cap is None else max(cap - sent, 0.0)

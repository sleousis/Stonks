"""What the live safeguards see of a book at a real broker (roadmap 19.6).

:class:`LiveContext` rides on ``RiskContext.live``. It is ``None`` in
backtests and paper books, and every live rule then does nothing, so
backtests stay identical.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from stonks.accounts.rules import AccountRuleInputs, InstrumentFacts
from stonks.execution.brokers.base import AccountReader, LiveAccountState, Quote, QuoteSource
from stonks.logging import get_logger
from stonks.production.ledger import ledger_columns
from stonks.production.live.stages import LiveStage, get_stage
from stonks.production.live.trades import ClosedTrade, closed_trades
from stonks.production.rules._account_settings import AccountRulesSettings
from stonks.store.state import SqliteState

_log = get_logger("stonks.production.live.context")


@dataclass(frozen=True)
class LiveContext:
    portfolio_id: str
    #: The amount the owner lets Stonks trade in this portfolio, in the
    #: account's base currency. ``None``: no allocation set, nothing opens.
    allocation: float | None = None
    #: The portfolio's live stage (roadmap 19.9). The safeguards run at
    #: every stage, so the broker paper soak exercises them.
    stage: LiveStage = "sim_paper"
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
    #: The book's recently closed trades, for the protections.
    closed_trades: Sequence[ClosedTrade] = ()

    def room_left(self, cap: float | None, sent: float) -> float | None:
        """What is left of a daily ``cap`` after ``sent`` (``None``: no cap)."""
        return None if cap is None else max(cap - sent, 0.0)


# ---- building one ----------------------------------------------------------------


def sent_notional_today(
    state: SqliteState, day: date, *, portfolio_ids: Sequence[str] | None = None
) -> float:
    """Notional of the opening orders created on ``day`` that were not
    rejected or cancelled: of ``portfolio_ids``, or of everyone (``None``).
    An order is valued at its limit, else its decision price."""
    cols = ledger_columns(state, "orders")
    price = (
        "COALESCE(limit_price, decision_price, 0)"
        if "decision_price" in cols
        else ("COALESCE(limit_price, 0)")
    )
    opening = (
        "(position_effect = 'open' OR (position_effect IS NULL AND side = 'buy'))"
        if "position_effect" in cols
        else "side = 'buy'"
    )
    sql = (
        f"SELECT COALESCE(SUM(quantity * {price}), 0) AS notional FROM orders"
        f" WHERE substr(created_at, 1, 10) = ? AND {opening}"
        " AND status NOT IN ('rejected', 'cancelled')"
    )
    params: list[Any] = [day.isoformat()]
    if portfolio_ids is not None:
        if not portfolio_ids or "portfolio_id" not in cols:
            return 0.0
        sql += f" AND portfolio_id IN ({','.join('?' for _ in portfolio_ids)})"
        params += list(portfolio_ids)
    return float(state.sql(sql, params)[0]["notional"])


def _owner_portfolios(state: SqliteState, portfolio_id: str) -> list[str]:
    rows = state.sql(
        "SELECT id FROM portfolios WHERE owner_id = (SELECT owner_id FROM portfolios WHERE id = ?)",
        [portfolio_id],
    )
    return [r["id"] for r in rows] or [portfolio_id]


def instrument_facts(lake: Any, tickers: Sequence[str]) -> dict[str, InstrumentFacts]:
    """Security type, ISIN, currency and the latest shares outstanding per
    ticker from the lake (missing tickers are left out)."""
    if lake is None or not tickers:
        return {}
    df = lake.sql(
        "SELECT i.id, i.security_type, i.isin, i.currency,"
        " (SELECT s.shares FROM shares_outstanding s WHERE s.ticker = i.id"
        "  ORDER BY s.date DESC LIMIT 1) AS shares"
        " FROM instruments i WHERE i.id = ANY(?)",
        [list(tickers)],
    )
    out: dict[str, InstrumentFacts] = {}
    for row in df.itertuples(index=False):
        shares = row.shares
        out[str(row.id)] = InstrumentFacts(
            security_type=row.security_type if isinstance(row.security_type, str) else None,
            isin=row.isin if isinstance(row.isin, str) else None,
            currency=row.currency if isinstance(row.currency, str) else None,
            shares_outstanding=float(shares) if shares is not None and shares == shares else None,
        )
    return out


def build_live_context(
    state: SqliteState,
    portfolio_id: str,
    as_of: date,
    *,
    broker: object | None = None,
    tickers: Sequence[str] = (),
    lake: Any = None,
    account_settings: AccountRulesSettings | None = None,
    stage: LiveStage | None = None,
    external_positions: Mapping[str, float] | None = None,
    shortable: Mapping[str, float | None] | None = None,
) -> LiveContext:
    """The live context of ``portfolio_id`` for a run that executes on
    ``as_of``. The broker's account and quotes are read through its
    optional capabilities (``AccountReader``, ``QuoteSource``). A read that
    fails leaves the field empty: the rules then refuse to open. ``stage``
    defaults to the portfolio's stored stage."""
    from stonks.accounts.rules.inputs import load_account_inputs
    from stonks.production.live.allocation import get_allocation

    account: LiveAccountState | None = None
    if isinstance(broker, AccountReader):
        try:
            account = broker.fetch_account()
        except Exception as exc:
            _log.warning("live.account_unreadable", portfolio_id=portfolio_id, error=str(exc))
    quotes: dict[str, Quote] = {}
    if isinstance(broker, QuoteSource) and tickers:
        try:
            quotes = dict(broker.quotes(list(tickers)))
        except Exception as exc:
            _log.warning("live.quotes_unreadable", portfolio_id=portfolio_id, error=str(exc))
    allocation = get_allocation(state, portfolio_id)
    inputs = load_account_inputs(
        state,
        portfolio_id,
        as_of,
        account_settings or AccountRulesSettings(),
        account=account,
        instruments=instrument_facts(lake, tickers),
        shortable=shortable,
    )
    return LiveContext(
        portfolio_id=portfolio_id,
        allocation=None if allocation is None else allocation.amount,
        stage=stage or get_stage(state, portfolio_id),
        account=account,
        quotes=quotes,
        sent_today=sent_notional_today(state, as_of, portfolio_ids=[portfolio_id]),
        sent_today_user=sent_notional_today(
            state, as_of, portfolio_ids=_owner_portfolios(state, portfolio_id)
        ),
        sent_today_global=sent_notional_today(state, as_of),
        external_positions=dict(external_positions or {}),
        account_rules=inputs,
        closed_trades=tuple(closed_trades(state, portfolio_id, as_of)),
    )

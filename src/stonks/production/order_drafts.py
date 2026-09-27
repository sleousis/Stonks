"""Order drafts (roadmap 20.4 safety): an order proposed, not placed.

The assistant never places an order. It may only create a draft, which a
person approves in the web app with a fresh second factor. Only then the
draft becomes a manual order and goes through every check
(:mod:`stonks.production.manual`). Deterministic code decides, the model
only proposes:

- the ticker must be an instrument the lake knows, with a recent close;
- the server computes the reference price (the latest close) and the
  notional, never the caller;
- a limit price must sit inside the price band around the reference;
- an :class:`Envelope` limits what a source may draft: an optional ticker
  allowlist, a notional cap per order and per day, and how long a draft
  stays approvable;
- the same retry key returns the draft already made, so a retried write
  never makes two;
- the kill switch cancels pending drafts (:func:`cancel_drafts`).

Any failed check raises :class:`DraftRefused`: nothing is created.
"""

from __future__ import annotations

import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from stonks.logging import get_logger
from stonks.production.prices import load_prices
from stonks.store.lake import DuckDBLake
from stonks.store.state import SqliteState

_log = get_logger("stonks.production.order_drafts")

DraftSource = Literal["assistant", "console", "mcp"]
DraftStatus = Literal["pending", "placed", "rejected", "expired", "cancelled"]
OPEN_STATUSES = ("pending",)


class DraftRefused(ValueError):
    """The draft was not created (or cannot change state)."""


class DraftNotFound(LookupError):
    """No such draft for this person."""


@dataclass(frozen=True)
class Envelope:
    """What one source may draft. ``None`` caps are unlimited."""

    allowed_tickers: frozenset[str] | None = None
    max_order_notional: float | None = None
    max_day_notional: float | None = None
    #: Largest distance of a limit price from the reference, as a fraction.
    price_band: float = 0.05
    ttl: timedelta = timedelta(hours=24)
    max_price_staleness_days: int = 5


@dataclass(frozen=True)
class DraftRequest:
    owner_id: str
    portfolio_id: str
    source: DraftSource
    retry_key: str
    ticker: str
    side: Literal["buy", "sell"]
    quantity: float
    reason: str
    order_type: Literal["market", "limit"] = "market"
    limit_price: float | None = None
    conversation_id: str | None = None


@dataclass(frozen=True)
class Draft:
    id: str
    owner_id: str
    portfolio_id: str
    source: str
    conversation_id: str | None
    retry_key: str
    ticker: str
    side: str
    quantity: float
    order_type: str
    limit_price: float | None
    reason: str
    reference_price: float
    notional: float
    status: str
    client_id: str | None
    created_at: str
    expires_at: str
    decided_at: str | None
    decided_by: str | None
    decision_note: str | None

    @classmethod
    def from_row(cls, row: Any) -> Draft:
        return cls(**{k: row[k] for k in cls.__dataclass_fields__})


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def create_draft(
    state: SqliteState,
    lake: DuckDBLake,
    request: DraftRequest,
    envelope: Envelope,
    *,
    now: datetime | None = None,
) -> tuple[Draft, bool]:
    """``(draft, created)``: the new draft, or the one this retry key made
    before (``created`` False). Raises :class:`DraftRefused`."""
    now = now or datetime.now(UTC)
    existing = state.sql(
        "SELECT * FROM order_drafts WHERE owner_id = ? AND retry_key = ?",
        [request.owner_id, request.retry_key],
    )
    if existing:
        return Draft.from_row(existing[0]), False
    ticker = request.ticker.strip().upper()
    if request.quantity <= 0:
        raise DraftRefused("the quantity must be positive")
    if len(request.reason.strip()) < 3:
        raise DraftRefused("say why (a reason of at least 3 characters)")
    if envelope.allowed_tickers is not None and ticker not in envelope.allowed_tickers:
        raise DraftRefused(f"{ticker} is not on the allowed list for drafts")
    known = lake.sql("SELECT 1 FROM instruments WHERE id = ? LIMIT 1", [ticker])
    priced = load_prices(
        lake, [ticker], [], now.date(), max_staleness_days=envelope.max_price_staleness_days
    )
    reference = priced.prices.get(ticker)
    if reference is None or ticker not in priced.fresh or reference <= 0:
        raise DraftRefused(f"no recent close for {ticker}: resolve the ticker first")
    if known.empty:
        raise DraftRefused(f"{ticker} is not a known instrument")
    if request.order_type == "limit":
        if request.limit_price is None or request.limit_price <= 0:
            raise DraftRefused("a limit order needs a positive limit price")
        distance = abs(request.limit_price / reference - 1.0)
        if distance > envelope.price_band:
            raise DraftRefused(
                f"limit {request.limit_price:g} is {distance:.1%} from the latest close "
                f"{reference:g}, outside the {envelope.price_band:.0%} band"
            )
    elif request.limit_price is not None:
        raise DraftRefused("a market order takes no limit price")
    notional = float(request.quantity) * float(reference)
    if envelope.max_order_notional is not None and notional > envelope.max_order_notional:
        raise DraftRefused(
            f"notional {notional:,.2f} is above the per-order cap "
            f"{envelope.max_order_notional:,.2f}"
        )
    if envelope.max_day_notional is not None:
        used = _day_notional(state, request.owner_id, request.source, now)
        if used + notional > envelope.max_day_notional:
            raise DraftRefused(
                f"today's drafts would reach {used + notional:,.2f}, above the daily cap "
                f"{envelope.max_day_notional:,.2f}"
            )
    draft_id = f"od_{secrets.token_hex(8)}"
    state.execute(
        "INSERT INTO order_drafts (id, owner_id, portfolio_id, source, conversation_id,"
        " retry_key, ticker, side, quantity, order_type, limit_price, reason, reference_price,"
        " notional, status, created_at, expires_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
        [
            draft_id,
            request.owner_id,
            request.portfolio_id,
            request.source,
            request.conversation_id,
            request.retry_key,
            ticker,
            request.side,
            float(request.quantity),
            request.order_type,
            request.limit_price,
            request.reason.strip(),
            float(reference),
            notional,
            _iso(now),
            _iso(now + envelope.ttl),
        ],
    )
    _log.info(
        "order_draft.created",
        draft_id=draft_id,
        source=request.source,
        ticker=ticker,
        notional=round(notional, 2),
    )
    return get_draft(state, request.owner_id, draft_id), True


def _day_notional(state: SqliteState, owner_id: str, source: str, now: datetime) -> float:
    rows = state.sql(
        "SELECT COALESCE(SUM(notional), 0) AS n FROM order_drafts WHERE owner_id = ?"
        " AND source = ? AND substr(created_at, 1, 10) = ? AND status IN ('pending', 'placed')",
        [owner_id, source, now.date().isoformat()],
    )
    return float(rows[0]["n"])


def get_draft(state: SqliteState, owner_id: str, draft_id: str) -> Draft:
    rows = state.sql(
        "SELECT * FROM order_drafts WHERE id = ? AND owner_id = ?", [draft_id, owner_id]
    )
    if not rows:
        raise DraftNotFound(f"no order draft {draft_id!r}")
    return Draft.from_row(rows[0])


def list_drafts(
    state: SqliteState,
    owner_id: str,
    *,
    status: str | None = None,
    now: datetime | None = None,
) -> list[Draft]:
    """A person's drafts, newest first (expired ones are marked first)."""
    expire_drafts(state, now=now)
    where, params = "owner_id = ?", [owner_id]
    if status is not None:
        where += " AND status = ?"
        params.append(status)
    rows = state.sql(
        f"SELECT * FROM order_drafts WHERE {where} ORDER BY created_at DESC, id DESC", params
    )
    return [Draft.from_row(r) for r in rows]


def expire_drafts(state: SqliteState, *, now: datetime | None = None) -> int:
    now = now or datetime.now(UTC)
    cur = state.execute(
        "UPDATE order_drafts SET status = 'expired', decided_at = ?, decision_note = 'expired'"
        " WHERE status = 'pending' AND expires_at <= ?",
        [_iso(now), _iso(now)],
    )
    return int(cur.rowcount or 0)


def claim_draft(
    state: SqliteState,
    owner_id: str,
    draft_id: str,
    status: DraftStatus,
    *,
    actor: str,
    note: str | None = None,
    now: datetime | None = None,
) -> Draft:
    """Move a pending draft to ``status`` once (a second call is refused)."""
    now = now or datetime.now(UTC)
    expire_drafts(state, now=now)
    draft = get_draft(state, owner_id, draft_id)
    cur = state.execute(
        "UPDATE order_drafts SET status = ?, decided_at = ?, decided_by = ?, decision_note = ?"
        " WHERE id = ? AND owner_id = ? AND status = 'pending'",
        [status, _iso(now), actor, note, draft_id, owner_id],
    )
    if not cur.rowcount:
        raise DraftRefused(f"draft {draft_id} is {draft.status}, not pending")
    return get_draft(state, owner_id, draft_id)


def mark_placed(state: SqliteState, draft_id: str, client_id: str) -> None:
    state.execute("UPDATE order_drafts SET client_id = ? WHERE id = ?", [client_id, draft_id])


def cancel_drafts(
    state: SqliteState,
    *,
    reason: str,
    user_id: str | None = None,
    portfolio_ids: Sequence[str] = (),
    everyone: bool = False,
    now: datetime | None = None,
) -> int:
    """Cancel pending drafts (the kill switch): every draft (``everyone``),
    a person's, or those of some portfolios. Returns how many."""
    now = now or datetime.now(UTC)
    clauses: list[str] = []
    params: list[Any] = []
    if not everyone:
        if user_id is not None:
            clauses.append("owner_id = ?")
            params.append(user_id)
        if portfolio_ids:
            clauses.append(f"portfolio_id IN ({','.join('?' for _ in portfolio_ids)})")
            params.extend(portfolio_ids)
        if not clauses:
            return 0
    where = " OR ".join(clauses) if clauses else "1 = 1"
    cur = state.execute(
        "UPDATE order_drafts SET status = 'cancelled', decided_at = ?, decided_by = 'system',"
        f" decision_note = ? WHERE status = 'pending' AND ({where})",
        [_iso(now), reason, *params],
    )
    count = int(cur.rowcount or 0)
    if count:
        _log.warning("order_drafts.cancelled", count=count, reason=reason)
    return count

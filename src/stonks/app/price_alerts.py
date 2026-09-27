"""PriceAlertService: your own price alert rules (roadmap 20.2).

A rule watches one ticker, or every ticker of one of your watchlists, for a
crossing of a level or a move by a percent over a window. The
``price_alerts`` scheduler job checks every rule after the prices are
ingested (:mod:`stonks.price_alerts`) and sends the firings through the
notification router: your feed, push, email, Telegram and your webhook,
with your quiet hours and preferences.

- Every call is scoped to the caller: another person's rule reads as
  missing (404), admins included.
- Reading needs ``data.read``. Creating, changing and deleting need
  ``notifications.manage`` (like your other notification settings).
- Running the check by hand (``evaluate``) is an operator action
  (``operations.run``), used by the scheduler's API backend.
"""

from __future__ import annotations

import re
import secrets
import sqlite3
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from stonks.accounts.audit import iso_now
from stonks.app.context import AppContext
from stonks.app.errors import NotFoundError, ValidationError
from stonks.app.pagination import Page
from stonks.auth.policy import Permission, require
from stonks.auth.principal import Principal
from stonks.notify.events import Event
from stonks.price_alerts import RunSummary, run_price_alerts
from stonks.store.state import SqliteState

#: Most rules one person may keep.
MAX_RULES = 200

_TICKER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.\-_^=]{0,39}$")

Condition = Literal["crosses_above", "crosses_below", "moves_pct"]


class PriceAlertView(BaseModel):
    id: str
    name: str | None
    target_kind: Literal["ticker", "watchlist"]
    ticker: str | None
    watchlist_id: str | None
    condition: Condition
    level: float | None
    pct: float | None
    window_days: int | None
    enabled: bool
    #: The price and time the rule last checked, per ticker.
    last_seen: dict[str, dict[str, Any]] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class _RuleFields(BaseModel):
    model_config = ConfigDict(extra="forbid")

    level: float | None = Field(default=None, gt=0, description="The price to watch (crossings).")
    pct: float | None = Field(
        default=None, gt=0, le=1000, description="Percent move, either way (moves_pct)."
    )
    window_days: int | None = Field(
        default=None, ge=1, le=365, description="Calendar days the move is measured over."
    )


class PriceAlertCreate(_RuleFields):
    """A rule on one ticker (``ticker``) or on one of your watchlists
    (``watchlist_id``), not both."""

    name: str | None = Field(default=None, max_length=80)
    ticker: str | None = None
    watchlist_id: str | None = Field(default=None, max_length=64)
    condition: Condition
    enabled: bool = True

    @field_validator("ticker")
    @classmethod
    def _ticker(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip().upper()
        if not _TICKER.match(v):
            raise ValueError(f"{v!r} is not an instrument id")
        return v

    @model_validator(mode="after")
    def _shape(self) -> PriceAlertCreate:
        if (self.ticker is None) == (self.watchlist_id is None):
            raise ValueError("give a ticker or a watchlist_id, not both")
        _check_condition(self.condition, self.level, self.pct, self.window_days)
        return self


class PriceAlertUpdate(_RuleFields):
    """Change the name, the thresholds or switch the rule on or off. The
    target and the condition stay (make a new rule for another one)."""

    name: str | None = Field(default=None, max_length=80)
    enabled: bool | None = None


class PriceAlertEventView(BaseModel):
    id: int
    rule_id: str
    ticker: str
    observed_at: str
    price: float
    detail: str
    created_at: datetime


class PriceAlertRunView(BaseModel):
    as_of: date
    rules: int
    checked: int
    fired: int
    published: int
    skipped_no_price: int


def _check_condition(
    condition: str, level: float | None, pct: float | None, window_days: int | None
) -> None:
    if condition in ("crosses_above", "crosses_below"):
        if level is None:
            raise ValueError(f"{condition} needs a level")
    elif pct is None or window_days is None:
        raise ValueError("moves_pct needs pct and window_days")


def _view(row: sqlite3.Row, seen: dict[str, dict[str, Any]]) -> PriceAlertView:
    r = dict(row)
    return PriceAlertView(
        id=r["id"],
        name=r["name"],
        target_kind=r["target_kind"],
        ticker=r["ticker"],
        watchlist_id=r["watchlist_id"],
        condition=r["condition"],
        level=r["level"],
        pct=r["pct"],
        window_days=r["window_days"],
        enabled=bool(r["enabled"]),
        last_seen=seen,
        created_at=datetime.fromisoformat(r["created_at"]),
        updated_at=datetime.fromisoformat(r["updated_at"]),
    )


class PriceAlertService:
    def __init__(
        self,
        context: AppContext,
        *,
        publisher: Callable[[SqliteState], Callable[[Event], Any]] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._ctx = context
        self._publisher = publisher
        self._clock = clock or (lambda: datetime.now(UTC))

    # ---- rules -------------------------------------------------------------------------

    def list(self, principal: Principal) -> list[PriceAlertView]:
        """Your rules, oldest first."""
        require(principal, Permission.READ)
        with self._ctx.state() as state:
            rows = state.sql(
                "SELECT * FROM price_alert_rules WHERE owner_id = ? ORDER BY created_at, id",
                [principal.user_id],
            )
            seen = self._seen(state, [r["id"] for r in rows])
        return [_view(r, seen.get(r["id"], {})) for r in rows]

    def get(self, principal: Principal, alert_id: str) -> PriceAlertView:
        require(principal, Permission.READ)
        with self._ctx.state() as state:
            row = self._owned(state, principal, alert_id)
            return _view(row, self._seen(state, [alert_id]).get(alert_id, {}))

    def create(self, principal: Principal, body: PriceAlertCreate) -> PriceAlertView:
        require(principal, Permission.NOTIFICATIONS_MANAGE)
        now = iso_now()
        rid = f"pal_{secrets.token_hex(6)}"
        with self._ctx.state() as state, state.transaction():
            count = int(
                state.sql(
                    "SELECT COUNT(*) FROM price_alert_rules WHERE owner_id = ?",
                    [principal.user_id],
                )[0][0]
            )
            if count >= MAX_RULES:
                raise ValidationError(f"you can keep at most {MAX_RULES} price alerts")
            if body.watchlist_id is not None:
                owned = state.sql(
                    "SELECT 1 FROM watchlists WHERE id = ? AND owner_id = ?",
                    [body.watchlist_id, principal.user_id],
                )
                if not owned:
                    raise NotFoundError(f"no watchlist with id {body.watchlist_id!r}")
            state.execute(
                "INSERT INTO price_alert_rules (id, owner_id, name, target_kind, ticker,"
                " watchlist_id, condition, level, pct, window_days, enabled, created_at,"
                " updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    rid,
                    principal.user_id,
                    (body.name or "").strip() or None,
                    "ticker" if body.ticker else "watchlist",
                    body.ticker,
                    body.watchlist_id,
                    body.condition,
                    body.level,
                    body.pct,
                    body.window_days,
                    int(body.enabled),
                    now,
                    now,
                ],
            )
            return _view(self._owned(state, principal, rid), {})

    def update(self, principal: Principal, alert_id: str, body: PriceAlertUpdate) -> PriceAlertView:
        require(principal, Permission.NOTIFICATIONS_MANAGE)
        with self._ctx.state() as state, state.transaction():
            row = dict(self._owned(state, principal, alert_id))
            fields = body.model_fields_set
            level = body.level if "level" in fields else row["level"]
            pct = body.pct if "pct" in fields else row["pct"]
            window = body.window_days if "window_days" in fields else row["window_days"]
            try:
                _check_condition(row["condition"], level, pct, window)
            except ValueError as exc:
                raise ValidationError(str(exc)) from None
            name = ((body.name or "").strip() or None) if "name" in fields else row["name"]
            enabled = row["enabled"] if body.enabled is None else int(body.enabled)
            state.execute(
                "UPDATE price_alert_rules SET name = ?, level = ?, pct = ?, window_days = ?,"
                " enabled = ?, updated_at = ? WHERE id = ?",
                [name, level, pct, window, enabled, iso_now(), alert_id],
            )
            if any(f in fields for f in ("level", "pct", "window_days")):
                # New thresholds start from the next price, not an old one.
                state.execute("DELETE FROM price_alert_state WHERE rule_id = ?", [alert_id])
            return _view(self._owned(state, principal, alert_id), {})

    def delete(self, principal: Principal, alert_id: str) -> None:
        require(principal, Permission.NOTIFICATIONS_MANAGE)
        with self._ctx.state() as state, state.transaction():
            self._owned(state, principal, alert_id)
            state.execute("DELETE FROM price_alert_rules WHERE id = ?", [alert_id])

    def events(
        self, principal: Principal, *, rule_id: str | None, limit: int, offset: int
    ) -> Page[PriceAlertEventView]:
        """When your rules fired, newest first."""
        require(principal, Permission.READ)
        where = "owner_id = ?"
        params: list[Any] = [principal.user_id]
        if rule_id is not None:
            where += " AND rule_id = ?"
            params.append(rule_id)
        with self._ctx.state() as state:
            total = int(
                state.sql(f"SELECT COUNT(*) FROM price_alert_events WHERE {where}", params)[0][0]
            )
            rows = state.sql(
                f"SELECT * FROM price_alert_events WHERE {where} ORDER BY id DESC LIMIT ? OFFSET ?",
                [*params, limit, offset],
            )
        items = [
            PriceAlertEventView(
                id=r["id"],
                rule_id=r["rule_id"],
                ticker=r["ticker"],
                observed_at=r["observed_at"],
                price=float(r["price"]),
                detail=r["detail"],
                created_at=datetime.fromisoformat(r["created_at"]),
            )
            for r in rows
        ]
        return Page[PriceAlertEventView](items=items, total=total, limit=limit, offset=offset)

    # ---- the check ---------------------------------------------------------------------

    def evaluate(self, principal: Principal | None = None, as_of: date | None = None) -> RunSummary:
        """Check every enabled rule of every person now (the scheduler job,
        or an operator). ``principal=None`` is in-process."""
        if principal is not None:
            require(principal, Permission.OPERATIONS_RUN)
        now = self._clock()
        day = as_of or now.date()
        with self._ctx.state() as state, self._ctx.lake() as lake:
            return run_price_alerts(state, lake, as_of=day, publish=self._publish(state), now=now)

    def evaluate_view(self, principal: Principal, as_of: date | None = None) -> PriceAlertRunView:
        day = as_of or self._clock().date()
        out = self.evaluate(principal, day)
        return PriceAlertRunView(as_of=day, **out.as_dict())

    def _publish(self, state: SqliteState) -> Callable[[Event], Any]:
        if self._publisher is not None:
            return self._publisher(state)
        from stonks.notify.router import configured_router

        return configured_router(state).publish

    # ---- helpers -----------------------------------------------------------------------

    @staticmethod
    def _owned(state: SqliteState, principal: Principal, alert_id: str) -> sqlite3.Row:
        rows = state.sql(
            "SELECT * FROM price_alert_rules WHERE id = ? AND owner_id = ?",
            [alert_id, principal.user_id],
        )
        if not rows:
            raise NotFoundError(f"no price alert with id {alert_id!r}")
        return rows[0]

    @staticmethod
    def _seen(state: SqliteState, rule_ids: list[str]) -> dict[str, dict[str, dict[str, Any]]]:
        if not rule_ids:
            return {}
        marks = ",".join("?" for _ in rule_ids)
        rows = state.sql(f"SELECT * FROM price_alert_state WHERE rule_id IN ({marks})", rule_ids)
        out: dict[str, dict[str, dict[str, Any]]] = {}
        for r in rows:
            out.setdefault(r["rule_id"], {})[r["ticker"]] = {
                "price": float(r["last_price"]),
                "observed_at": r["last_observed_at"],
            }
        return out

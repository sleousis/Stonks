"""Maintenance margin monitoring of live margin accounts (roadmap 19.13).

The broker is the source of truth. Its cushion (excess liquidity over
equity) says how far the account is from a margin call, and the levels
come from ``[production.risk.rules.margin_call]``:

- ``ok``: nothing to do;
- ``warn`` (below ``warn_cushion``): a normal alert to the owner;
- ``reduce`` (below ``reduce_cushion``): a high-urgency alert. The next
  run of the live book closes positions until the cushion is back at
  ``restore_cushion`` (the ``margin_call`` risk rule), before the broker
  liquidates on its own;
- ``call`` (at or below 0): the broker may already be selling. A
  high-urgency alert.

Each read writes a ``margin_checks`` row: the tick of a live margin book
(``tick``) and the ``live_margin`` job during the session (``monitor``).
Alerts are deduplicated per portfolio, level and day. No amount or ticker
leaves the server in an alert.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from stonks.core.clock import SYSTEM_CLOCK, Clock, iso_now, today
from stonks.execution.brokers.base import LiveAccountState
from stonks.logging import get_logger
from stonks.production.rules.margin_call import MarginCallSettings
from stonks.store.state import SqliteState

_log = get_logger("stonks.production.live.margin")

TABLE = "margin_checks"
MarginLevel = Literal["ok", "warn", "reduce", "call"]
CheckSource = Literal["tick", "monitor"]
Publish = Callable[[Any], Any]


@dataclass(frozen=True)
class MarginCheck:
    portfolio_id: str
    checked_at: str
    source: CheckSource
    currency: str
    equity: float
    initial_margin: float
    maintenance_margin: float
    excess_liquidity: float | None
    available_funds: float | None
    buying_power: float | None
    cushion: float | None
    level: MarginLevel
    reported_type: str | None

    @property
    def margin_use(self) -> float | None:
        """Maintenance margin over equity (``None`` without equity)."""
        return self.maintenance_margin / self.equity if self.equity > 0 else None


def checks_enabled(state: SqliteState) -> bool:
    rows = state.sql("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", [TABLE])
    return bool(rows)


def margin_level(account: LiveAccountState, settings: MarginCallSettings) -> MarginLevel | None:
    """The account's level, or ``None`` when the broker sent no cushion."""
    cushion = account.cushion
    if cushion is None:
        return None
    reduce_at = settings.reduce_cushion
    warn_at = max(settings.warn_cushion, reduce_at)
    if cushion <= 0:
        return "call"
    if cushion < reduce_at:
        return "reduce"
    if cushion < warn_at:
        return "warn"
    return "ok"


def check_margin(
    state: SqliteState,
    portfolio_id: str,
    account: LiveAccountState,
    settings: MarginCallSettings,
    *,
    source: CheckSource,
    publish: Publish | None = None,
    clock: Clock = SYSTEM_CLOCK,
) -> MarginCheck | None:
    """Record a margin account's level and alert its owner when it is not
    ``ok``. ``None`` for a cash account or one with no cushion."""
    if account.account_type != "margin":
        return None
    level = margin_level(account, settings)
    if level is None:
        _log.warning("live.margin_unknown", portfolio_id=portfolio_id)
        return None
    check = MarginCheck(
        portfolio_id=portfolio_id,
        checked_at=iso_now(clock),
        source=source,
        currency=account.currency,
        equity=account.equity,
        initial_margin=account.initial_margin,
        maintenance_margin=account.maintenance_margin,
        excess_liquidity=account.excess_liquidity,
        available_funds=account.available_funds,
        buying_power=account.buying_power,
        cushion=account.cushion,
        level=level,
        reported_type=account.reported_type,
    )
    if checks_enabled(state):
        _record(state, check)
    if level != "ok":
        _log.warning(
            "live.margin_level", portfolio_id=portfolio_id, level=level, cushion=check.cushion
        )
        _alert(state, check, settings, publish, clock)
    return check


def _record(state: SqliteState, c: MarginCheck) -> None:
    state.execute(
        f"INSERT INTO {TABLE} (portfolio_id, checked_at, source, currency, equity,"
        " initial_margin, maintenance_margin, excess_liquidity, available_funds, buying_power,"
        " cushion, level, reported_type) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            c.portfolio_id,
            c.checked_at,
            c.source,
            c.currency,
            c.equity,
            c.initial_margin,
            c.maintenance_margin,
            c.excess_liquidity,
            c.available_funds,
            c.buying_power,
            c.cushion,
            c.level,
            c.reported_type,
        ],
    )


_TEXT: dict[MarginLevel, tuple[str, str]] = {
    "warn": (
        "Margin cushion is getting thin",
        "Keep an eye on it. Nothing is sold yet.",
    ),
    "reduce": (
        "Margin cushion too thin: reducing",
        "The next run of the live book closes positions until the cushion is back, before "
        "the broker sells on its own. New positions wait.",
    ),
    "call": (
        "Margin call",
        "The account is below its maintenance margin. The broker may sell positions now. "
        "Add cash or close positions at the broker. See the margin runbook.",
    ),
}


def _alert(
    state: SqliteState,
    check: MarginCheck,
    settings: MarginCallSettings,
    publish: Publish | None,
    clock: Clock,
) -> None:
    from stonks.notify.events import Audience, Event
    from stonks.notify.router import configured_router

    if check.level == "ok":
        return
    title, advice = _TEXT[check.level]
    rows = state.sql("SELECT name FROM portfolios WHERE id = ?", [check.portfolio_id])
    name = rows[0]["name"] if rows else check.portfolio_id
    cushion = 0.0 if check.cushion is None else check.cushion
    event = Event(
        category="risk",
        level="warning" if check.level == "warn" else "error",
        urgency="normal" if check.level == "warn" else "high",
        title=title,
        body=f"{name}: the margin cushion is {cushion:.0%} "
        f"(alert below {settings.warn_cushion:.0%}, reduce below "
        f"{settings.reduce_cushion:.0%}). {advice}",
        audience=Audience.owner_of(check.portfolio_id),
        dedupe_key=f"margin:{check.portfolio_id}:{check.level}:{today(clock).isoformat()}",
        deep_link=f"/profile/live/{check.portfolio_id}",
        portfolio_id=check.portfolio_id,
    )
    try:
        (publish or configured_router(state).publish)(event)
    except Exception as exc:  # an alert that fails never stops the check
        _log.error("live.margin_notify_failed", portfolio_id=check.portfolio_id, error=str(exc))


def latest_check(state: SqliteState, portfolio_id: str) -> MarginCheck | None:
    """The newest check of ``portfolio_id``, or ``None``."""
    got = recent_checks(state, portfolio_id, limit=1)
    return got[0] if got else None


def recent_checks(state: SqliteState, portfolio_id: str, *, limit: int = 20) -> list[MarginCheck]:
    """The newest checks first."""
    if not checks_enabled(state):
        return []
    rows = state.sql(
        f"SELECT * FROM {TABLE} WHERE portfolio_id = ? ORDER BY checked_at DESC, id DESC LIMIT ?",
        [portfolio_id, limit],
    )
    return [_from_row(r) for r in rows]


def _from_row(r: Any) -> MarginCheck:
    return MarginCheck(
        portfolio_id=r["portfolio_id"],
        checked_at=r["checked_at"],
        source=r["source"],
        currency=r["currency"],
        equity=float(r["equity"]),
        initial_margin=float(r["initial_margin"]),
        maintenance_margin=float(r["maintenance_margin"]),
        excess_liquidity=_opt(r["excess_liquidity"]),
        available_funds=_opt(r["available_funds"]),
        buying_power=_opt(r["buying_power"]),
        cushion=_opt(r["cushion"]),
        level=r["level"],
        reported_type=r["reported_type"],
    )


def _opt(value: Any) -> float | None:
    return None if value is None else float(value)


def margin_portfolios(state: SqliteState, portfolio_ids: Sequence[str]) -> list[str]:
    """Those of ``portfolio_ids`` whose account profile is a margin account."""
    from stonks.accounts.rules.profiles import get_profile

    out: list[str] = []
    for pid in portfolio_ids:
        profile = get_profile(state, pid)
        if profile is not None and profile.account_type == "margin":
            out.append(pid)
    return out

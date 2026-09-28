"""GatewayHealthService: the IB Gateways the ``broker_health`` job checks
(roadmap 19.4), for the Health page.

One row per gateway listed under ``[brokers.ibkr.gateways]`` or seen by the
job: connected or not, the last check and the last good one, how long it
has been down, and the auto subscriptions paused on its portfolios.

Gateway rows are system facts, so any reader sees them. Portfolios follow
the usual visibility rule: a person sees the names and paused books of
their own portfolios only, admins included. Others' paused books count in
``paused_elsewhere`` without names.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from stonks.app.context import AppContext
from stonks.auth.policy import Permission, require
from stonks.auth.principal import Principal
from stonks.production.broker_health import GatewayStatus, gateway_statuses
from stonks.store.state import SqliteState


class PausedBookView(BaseModel):
    subscription_id: str
    portfolio_id: str
    portfolio_name: str
    strategy_id: str
    #: Why auto paused (the gateway outage or fault).
    reason: str


class GatewayView(BaseModel):
    gateway: str
    mode: Literal["paper", "live"]
    #: ``False`` until the job has checked it once (see ``checked``).
    connected: bool
    #: Whether the job has checked this gateway yet.
    checked: bool
    last_check_at: str | None
    last_ok_at: str | None
    down_since: str | None
    consecutive_failures: int
    #: A real fault (``login_refused``, ``wrong_account``,
    #: ``competing_session``), or ``None``.
    fault: str | None
    detail: str | None
    latency_ms: float | None
    #: When the job paused the auto books of this gateway's portfolios.
    paused_at: str | None
    #: Your portfolios that trade through this gateway, by name.
    your_portfolios: list[str]
    #: Your auto subscriptions paused on this gateway's portfolios.
    paused_books: list[PausedBookView]
    #: Paused auto subscriptions in other people's portfolios (a count only).
    paused_elsewhere: int


class GatewayHealthView(BaseModel):
    #: Whether any gateway is listed in the settings.
    configured: bool
    gateways: list[GatewayView]


class GatewayHealthService:
    def __init__(self, context: AppContext) -> None:
        self._ctx = context

    def gateways(self, principal: Principal) -> GatewayHealthView:
        require(principal, Permission.READ)
        listed = self._ctx.settings.brokers.ibkr.gateways
        with self._ctx.state() as state:
            stored = {s.gateway: s for s in gateway_statuses(state)}
            names = sorted(set(listed) | set(stored))
            views = []
            for name in names:
                cfg = listed.get(name)
                portfolios = list(cfg.portfolios) if cfg is not None else []
                mode = cfg.mode if cfg is not None else stored[name].mode
                views.append(self._view(state, principal, name, mode, stored.get(name), portfolios))
        return GatewayHealthView(configured=bool(listed), gateways=views)

    def _view(
        self,
        state: SqliteState,
        principal: Principal,
        name: str,
        mode: Literal["paper", "live"],
        status: GatewayStatus | None,
        portfolios: list[str],
    ) -> GatewayView:
        mine = _owned(state, principal, portfolios)
        paused = _paused(state, portfolios)
        return GatewayView(
            gateway=name,
            mode=mode,
            connected=bool(status and status.connected),
            checked=status is not None,
            last_check_at=status.last_check_at if status else None,
            last_ok_at=status.last_ok_at if status else None,
            down_since=status.down_since if status else None,
            consecutive_failures=status.consecutive_failures if status else 0,
            fault=status.fault if status else None,
            detail=status.detail if status else None,
            latency_ms=status.latency_ms if status else None,
            paused_at=status.paused_at if status else None,
            your_portfolios=[mine[pid] for pid in portfolios if pid in mine],
            paused_books=[p for p in paused if p.portfolio_id in mine],
            paused_elsewhere=sum(1 for p in paused if p.portfolio_id not in mine),
        )


def _owned(state: SqliteState, principal: Principal, ids: list[str]) -> dict[str, str]:
    """``{id: name}`` of the portfolios in ``ids`` the caller owns."""
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    rows = state.sql(
        f"SELECT id, name FROM portfolios WHERE id IN ({marks}) AND owner_id = ?",
        [*ids, principal.scope.user_id],
    )
    return {r["id"]: r["name"] for r in rows}


def _paused(state: SqliteState, ids: list[str]) -> list[PausedBookView]:
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    rows = state.sql(
        "SELECT s.id, s.portfolio_id, p.name, s.strategy_id, s.paused_reason"
        " FROM subscriptions s JOIN portfolios p ON p.id = s.portfolio_id"
        f" WHERE s.portfolio_id IN ({marks}) AND s.mode IN ('approve', 'auto')"
        " AND s.paused_reason IS NOT NULL ORDER BY p.name, s.strategy_id",
        ids,
    )
    return [
        PausedBookView(
            subscription_id=r["id"],
            portfolio_id=r["portfolio_id"],
            portfolio_name=r["name"],
            strategy_id=r["strategy_id"],
            reason=r["paused_reason"],
        )
        for r in rows
    ]

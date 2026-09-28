"""StarterService — the starter set (complexity audit F19) for transports:
which starters exist and are registered, and installing them On trial.
See :mod:`stonks.starter`."""

from __future__ import annotations

from pydantic import BaseModel, Field

from stonks.accounts import Scope
from stonks.app.context import AppContext
from stonks.auth.errors import PermissionDenied
from stonks.auth.policy import Permission, require
from stonks.auth.principal import Principal
from stonks.starter import STARTER_UNIVERSE, STARTERS, install_starters

Who = Principal | Scope


class StarterStrategyView(BaseModel):
    id: str
    title: str
    summary: str
    status: str | None = Field(
        description="Its status when registered (shadow = On trial), null when not installed."
    )


class StarterView(BaseModel):
    strategies: list[StarterStrategyView]
    universe: list[str] = Field(description="The starter trading universe.")
    installed: bool = Field(description="Every starter is registered (in any status).")


class StarterInstallView(BaseModel):
    registered: list[str] = Field(description="Starters put On trial by this call.")
    skipped: list[str] = Field(description="Starters that were already registered.")
    universe: list[str] | None = Field(
        description="The trading universe this call set, or null when one was configured."
    )
    next_steps: list[str]


class StarterService:
    def __init__(self, context: AppContext) -> None:
        self._ctx = context

    def status(self, principal: Principal) -> StarterView:
        require(principal, Permission.READ)
        with self._ctx.state() as state:
            rows = state.sql(
                f"SELECT id, status FROM strategies WHERE id IN ({', '.join('?' for _ in STARTERS)})",
                [s.id for s in STARTERS],
            )
        found = {r["id"]: r["status"] for r in rows}
        return StarterView(
            strategies=[
                StarterStrategyView(
                    id=s.id, title=s.title, summary=s.summary, status=found.get(s.id)
                )
                for s in STARTERS
            ],
            universe=list(STARTER_UNIVERSE),
            installed=len(found) == len(STARTERS),
        )

    def install(self, who: Who) -> StarterInstallView:
        """Register the missing starters On trial (never approved) and set
        the trading universe when none is configured. Idempotent."""
        if isinstance(who, Principal):
            require(who, Permission.OPERATIONS_RUN)
            actor = who.actor
        elif who.is_service:
            actor = who.actor
        else:
            raise PermissionDenied("installing the starter set is for admins")
        with self._ctx.state() as state:
            done = install_starters(
                state,
                self._ctx.registry_on(state),
                actor=actor,
                current_universe=self._ctx.settings.production.universe,
            )
        self._ctx.invalidate_settings()
        steps = []
        if done.universe:
            steps.append(
                "Load about two years of prices for the starter universe, e.g. "
                f"`stonks ingest prices --tickers {','.join(done.universe)} --since <2 years ago>`."
            )
        if done.registered:
            steps.append(
                "The starters run their test books from the next trading run. Approve one on "
                "its strategy page once it passes the go-live check, so traders can follow it."
            )
        return StarterInstallView(
            registered=list(done.registered),
            skipped=list(done.skipped),
            universe=list(done.universe) if done.universe else None,
            next_steps=steps,
        )

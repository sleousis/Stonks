"""BriefingService: research-only briefings before the open and after the
close (roadmap 23.8). The rules live in :mod:`stonks.assistant.briefings`.

A briefing is one assistant turn per person, in a research-only
conversation of its own, as that person with the ``read`` scope only. The
answer (grounding-checked like any reply) goes out through the
notification router: the feed, Web Push, email and Telegram."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any

from pydantic import BaseModel

from stonks.accounts.users import UserRepository
from stonks.app.assistant import AssistantService
from stonks.app.context import AppContext
from stonks.assistant import briefings
from stonks.assistant.briefings import BriefingKind, BriefingPrefs
from stonks.assistant.guard import Gate
from stonks.assistant.loop import AgentLoop
from stonks.assistant.store import ConversationStore
from stonks.auth import ApiScope, Permission, Principal, require
from stonks.logging import get_logger
from stonks.notify.events import Audience, Event

_log = get_logger("stonks.app.briefings")

Publish = Callable[[Event], Any]


class BriefingPrefsView(BaseModel):
    #: The install sends briefings (``[assistant.briefings] enabled`` and an
    #: assistant endpoint). Your switches only act when this is true.
    available: bool
    pre_open: bool
    post_close: bool


class BriefingPrefsUpdate(BaseModel):
    pre_open: bool
    post_close: bool


class BriefingRunRequest(BaseModel):
    kind: BriefingKind
    as_of: date | None = None


class BriefingRunView(BaseModel):
    kind: BriefingKind
    as_of: date
    #: Why nothing ran (off, no assistant endpoint), else null.
    skipped: str | None = None
    people: int = 0
    sent: int = 0
    failed: int = 0


class BriefingService:
    def __init__(
        self,
        context: AppContext,
        assistant: AssistantService,
        *,
        publish: Publish | None = None,
    ) -> None:
        self._ctx = context
        self._assistant = assistant
        self._publish = publish

    @property
    def available(self) -> bool:
        cfg = self._ctx.settings.assistant
        return cfg.enabled and cfg.briefings.enabled

    # ---- prefs ------------------------------------------------------------------

    def prefs(self, principal: Principal) -> BriefingPrefsView:
        require(principal, Permission.READ)
        with self._ctx.state() as state:
            p = briefings.get_prefs(state, principal.user_id)
        return BriefingPrefsView(
            available=self.available, pre_open=p.pre_open, post_close=p.post_close
        )

    def set_prefs(self, principal: Principal, body: BriefingPrefsUpdate) -> BriefingPrefsView:
        require(principal, Permission.NOTIFICATIONS_MANAGE)
        with self._ctx.state() as state:
            briefings.set_prefs(
                state, principal.user_id, BriefingPrefs(body.pre_open, body.post_close)
            )
        return self.prefs(principal)

    # ---- the run ------------------------------------------------------------------

    async def run(self, app: Any, kind: BriefingKind, as_of: date | None = None) -> BriefingRunView:
        """Every person who turned ``kind`` on gets one briefing."""
        day = as_of or datetime.now(UTC).date()
        view = BriefingRunView(kind=kind, as_of=day)
        if not self._ctx.settings.assistant.briefings.enabled:
            return view.model_copy(update={"skipped": "briefings are off"})
        if not self._ctx.settings.assistant.enabled:
            return view.model_copy(update={"skipped": "no assistant endpoint"})
        with self._ctx.state() as state:
            people = briefings.subscribers(state, kind)
            users = {uid: UserRepository(state).get(uid) for uid in people}
        sent = failed = 0
        for uid, user in users.items():
            try:
                text = await self._brief(app, user, kind, day)
                if text:
                    self._deliver(uid, kind, day, text)
                    sent += 1
                else:
                    failed += 1
            except Exception as exc:  # one person's failure never stops the others
                failed += 1
                _log.warning("briefing.failed", user_id=uid, kind=kind, error=type(exc).__name__)
        return view.model_copy(update={"people": len(people), "sent": sent, "failed": failed})

    async def _brief(self, app: Any, user: Any, kind: BriefingKind, day: date) -> str:
        cfg = self._assistant.config
        # read scope only and a research-only gate: no write can happen
        principal = Principal.create(
            user_id=user.id,
            kind=user.kind,
            role=user.role,
            scopes=[ApiScope.READ],
            mfa_fresh=False,
            via="assistant",
        )
        store = ConversationStore(self._ctx.state)
        title = f"{briefings.TITLES[kind]} {day.isoformat()}"
        conv = store.create(user.id, title, research_only=True)
        bridge = self._assistant.bridge_factory(app, principal)
        loop = AgentLoop(
            self._assistant.model_factory(cfg),
            bridge,
            store,
            cfg,
            gate=Gate(research_only=True, order_tools=False, reason="briefing"),
            owner_id=user.id,
        )
        try:
            async for _ in loop.send(conv.id, briefings.PROMPTS[kind]):
                pass
        finally:
            await bridge.aclose()
        answers = [m for m in store.messages(conv.id) if m.role == "assistant" and m.content]
        return answers[-1].content.strip() if answers else ""

    def _deliver(self, user_id: str, kind: BriefingKind, day: date, text: str) -> None:
        event = Event(
            category="system",
            title=f"{briefings.TITLES[kind]}: your briefing",
            body=text,
            audience=Audience.users(user_id),
            dedupe_key=f"briefing:{kind}:{day.isoformat()}",
            deep_link="/assistant",
        )
        if self._publish is not None:
            self._publish(event)
            return
        from stonks.notify.router import configured_router

        with self._ctx.state() as state:
            configured_router(state).publish(event)

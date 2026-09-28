"""The in-app AI assistant (roadmap 20.4). Conversations are yours only.
Sending a message or deciding a pending action answers a server-sent event
stream of :class:`~stonks.app.assistant.AssistantEventView`. The chat
itself needs ``data.read``: each tool the assistant calls checks its own
route's permission, as the signed-in person, and never passes step-up."""

from __future__ import annotations

from collections.abc import AsyncIterable, AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from fastapi.sse import EventSourceResponse, ServerSentEvent

from stonks.api.deps import PageDep, PrincipalDep, ServicesDep, needs
from stonks.api.errors import PROBLEM_RESPONSES
from stonks.api.routers._jobs_common import JOB_CREATED, accepted
from stonks.app.assistant import (
    ActionDecision,
    AssistantEventView,
    AssistantStatusView,
    ConversationCreate,
    ConversationDetailView,
    ConversationView,
    MessageCreate,
    TurnView,
    event_view,
)
from stonks.app.assistant_research import (
    ResearchSessionDetailView,
    ResearchSessionView,
    ResearchStart,
)
from stonks.app.briefings import (
    BriefingPrefsUpdate,
    BriefingPrefsView,
    BriefingRunRequest,
    BriefingRunView,
)
from stonks.app.jobs import Job
from stonks.app.pagination import Page, page_of
from stonks.assistant.loop import AssistantEvent
from stonks.auth import Permission

router = APIRouter(prefix="/api/assistant", tags=["assistant"], responses=PROBLEM_RESPONSES)

Events = AsyncIterator[AssistantEvent]


@router.get("/status", response_model=AssistantStatusView, operation_id="getAssistantStatus")
def assistant_status(services: ServicesDep, principal: PrincipalDep) -> AssistantStatusView:
    """Whether a model endpoint is configured, its model and the turn limits."""
    return services.assistant.status(principal)


@router.get(
    "/conversations",
    response_model=Page[ConversationView],
    operation_id="listAssistantConversations",
)
def list_conversations(
    services: ServicesDep, principal: PrincipalDep, page: PageDep
) -> Page[ConversationView]:
    """Your conversations, most recent first."""
    return services.assistant.list(principal, limit=page.limit, offset=page.offset)


@router.post(
    "/conversations",
    response_model=ConversationView,
    status_code=201,
    operation_id="createAssistantConversation",
    dependencies=needs(Permission.READ),
)
def create_conversation(
    body: ConversationCreate, services: ServicesDep, principal: PrincipalDep
) -> ConversationView:
    return services.assistant.create(principal, body)


@router.get(
    "/conversations/{conversation_id}",
    response_model=ConversationDetailView,
    operation_id="getAssistantConversation",
)
def get_conversation(
    conversation_id: str, services: ServicesDep, principal: PrincipalDep
) -> ConversationDetailView:
    """One of your conversations with its messages and pending actions."""
    return services.assistant.get(principal, conversation_id)


@router.delete(
    "/conversations/{conversation_id}",
    status_code=204,
    response_class=Response,
    operation_id="deleteAssistantConversation",
    dependencies=needs(Permission.READ),
)
def delete_conversation(
    conversation_id: str, services: ServicesDep, principal: PrincipalDep
) -> Response:
    services.assistant.delete(principal, conversation_id)
    return Response(status_code=204)


@router.get(
    "/conversations/{conversation_id}/turns",
    response_model=Page[TurnView],
    operation_id="listAssistantTurns",
)
def list_turns(
    conversation_id: str, services: ServicesDep, principal: PrincipalDep, page: PageDep
) -> Page[TurnView]:
    """The trace of one of your conversations: each turn's model, prompt
    version, tool calls and results, and the order drafts it made."""
    return page_of(services.assistant.turns(principal, conversation_id), page)


@router.delete(
    "/freeze",
    status_code=204,
    response_class=Response,
    operation_id="clearAssistantFreeze",
    dependencies=needs(Permission.KILLSWITCH_RESUME),
)
def clear_freeze(services: ServicesDep, principal: PrincipalDep) -> Response:
    """Unfreeze your assistant after a burst of writes. Needs a fresh
    second factor."""
    services.assistant.clear_freeze(principal)
    return Response(status_code=204)


@router.get("/briefings/prefs", response_model=BriefingPrefsView, operation_id="getBriefingPrefs")
def get_briefing_prefs(services: ServicesDep, principal: PrincipalDep) -> BriefingPrefsView:
    """Your research-only briefings before the open and after the close,
    and whether this install sends them at all."""
    return services.briefings.prefs(principal)


@router.put(
    "/briefings/prefs",
    response_model=BriefingPrefsView,
    operation_id="setBriefingPrefs",
    dependencies=needs(Permission.NOTIFICATIONS_MANAGE),
)
def set_briefing_prefs(
    body: BriefingPrefsUpdate, services: ServicesDep, principal: PrincipalDep
) -> BriefingPrefsView:
    """Turn your briefings on or off. They come through your notification
    channels (the feed, push, email, Telegram)."""
    return services.briefings.set_prefs(principal, body)


@router.post(
    "/briefings/run",
    response_model=BriefingRunView,
    operation_id="runBriefings",
    dependencies=needs(Permission.OPERATIONS_RUN),
)
async def run_briefings(
    body: BriefingRunRequest, request: Request, services: ServicesDep
) -> BriefingRunView:
    """Send one kind of briefing to everyone who turned it on now (the
    scheduler runs this before the open and after the close). Research
    only: each briefing runs with read tools only."""
    return await services.briefings.run(request.app, body.kind, body.as_of)


def _message_turn(
    conversation_id: str,
    body: MessageCreate,
    request: Request,
    services: ServicesDep,
    principal: PrincipalDep,
) -> Events:
    # Checked before the stream starts, so a refusal is a normal problem.
    return services.assistant.open_message(principal, request.app, conversation_id, body)


def _decision_turn(
    conversation_id: str,
    action_id: str,
    body: ActionDecision,
    request: Request,
    services: ServicesDep,
    principal: PrincipalDep,
) -> Events:
    return services.assistant.open_decision(
        principal, request.app, conversation_id, action_id, body
    )


async def _sse(events: Events) -> AsyncIterator[ServerSentEvent]:
    seq = 0
    async for event in events:
        seq += 1
        yield ServerSentEvent(data=event_view(event), event=event.kind, id=str(seq))


@router.post(
    "/conversations/{conversation_id}/messages",
    response_class=EventSourceResponse,
    operation_id="sendAssistantMessage",
    dependencies=needs(Permission.READ),
)
async def send_message(
    events: Annotated[Events, Depends(_message_turn)],
) -> AsyncIterable[AssistantEventView]:
    """Send a message. The answer streams as events (text pieces, tool
    calls and results) and ends with ``done``. A write action stops the
    turn with ``confirm_required``: approve or reject it with the actions
    route."""
    async for item in _sse(events):
        yield item  # type: ignore[misc]  # ServerSentEvent wraps the typed payload


@router.post(
    "/conversations/{conversation_id}/actions/{action_id}",
    response_class=EventSourceResponse,
    operation_id="decideAssistantAction",
    dependencies=needs(Permission.READ),
)
async def decide_action(
    events: Annotated[Events, Depends(_decision_turn)],
) -> AsyncIterable[AssistantEventView]:
    """Approve (run) or reject a write action the assistant asked for. The
    turn then continues as a new event stream."""
    async for item in _sse(events):
        yield item  # type: ignore[misc]


# ---- the research loop (roadmap 22.9) ---------------------------------------------


@router.post(
    "/research",
    **JOB_CREATED,
    operation_id="startAssistantResearch",
    dependencies=needs(Permission.LAB_RUN),
)
def start_research(
    body: ResearchStart, services: ServicesDep, principal: PrincipalDep, response: Response
) -> Job:
    """Start a research session: the assistant proposes lab trials for your
    goal and runs them under the session's budgets. Every proposal is
    recorded with its hypothesis before it runs, every trial is counted in
    the trial ledger, and validation windows start after the model's
    training cutoff. It never registers or promotes a strategy. Follow the
    job, then read the session."""
    return accepted(services.research.start(principal, body), response)


@router.get(
    "/research",
    response_model=Page[ResearchSessionView],
    operation_id="listAssistantResearch",
)
def list_research(
    services: ServicesDep, principal: PrincipalDep, page: PageDep
) -> Page[ResearchSessionView]:
    """Your research sessions, newest first."""
    return services.research.list(principal, limit=page.limit, offset=page.offset)


@router.get(
    "/research/{session_id}",
    response_model=ResearchSessionDetailView,
    operation_id="getAssistantResearch",
)
def get_research(
    session_id: str, services: ServicesDep, principal: PrincipalDep
) -> ResearchSessionDetailView:
    """One of your research sessions with every proposal: its hypothesis,
    whether it ran or why not, and its lab run in the trial ledger."""
    return services.research.get(principal, session_id)

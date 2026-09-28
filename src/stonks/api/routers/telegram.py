"""Your Telegram chat link (roadmap 20.3). The code is shown once. Commands
run in the bot, as the linked user, never through these routes."""

from __future__ import annotations

from fastapi import APIRouter, Response

from stonks.api.deps import PrincipalDep, ServicesDep, needs
from stonks.api.errors import PROBLEM_RESPONSES
from stonks.app.telegram import TelegramLinkCodeView, TelegramLinkView
from stonks.auth import Permission

router = APIRouter(prefix="/api/telegram", tags=["telegram"], responses=PROBLEM_RESPONSES)


@router.get("/link", response_model=TelegramLinkView, operation_id="getTelegramLink")
def get_link(services: ServicesDep, principal: PrincipalDep) -> TelegramLinkView:
    """Whether the server has a bot, and which chat is linked to you."""
    return services.telegram.link(principal)


@router.post(
    "/link-code",
    status_code=201,
    response_model=TelegramLinkCodeView,
    operation_id="createTelegramLinkCode",
    dependencies=needs(Permission.NOTIFICATIONS_MANAGE),
)
def create_link_code(services: ServicesDep, principal: PrincipalDep) -> TelegramLinkCodeView:
    """A one-time code (valid for a few minutes). Send ``/link CODE`` to the
    bot from the chat to link. A new code retires your older ones. 503 when
    the server has no bot token."""
    return services.telegram.create_code(principal)


@router.delete(
    "/link",
    status_code=204,
    response_class=Response,
    operation_id="deleteTelegramLink",
    dependencies=needs(Permission.NOTIFICATIONS_MANAGE),
)
def delete_link(services: ServicesDep, principal: PrincipalDep) -> Response:
    """Unlink your chat. Nothing more is sent there. Idempotent."""
    services.telegram.unlink(principal)
    return Response(status_code=204)

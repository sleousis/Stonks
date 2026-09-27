"""The live engine status (roadmap 21.3.4): engine, stream health, latency
and the dead-man, read from the engine's status row. Read only
(``data.read``). The Prometheus families sit on ``GET /metrics``."""

from __future__ import annotations

from fastapi import APIRouter

from stonks.api.deps import PrincipalDep, ServicesDep, needs
from stonks.api.errors import PROBLEM_RESPONSES
from stonks.app.stream import StreamService, StreamStatusView
from stonks.auth import Permission

router = APIRouter(prefix="/api/stream", tags=["stream"], responses=PROBLEM_RESPONSES)


@router.get(
    "/status",
    response_model=StreamStatusView,
    operation_id="getStreamStatus",
    dependencies=needs(Permission.READ),
)
def get_stream_status(services: ServicesDep, principal: PrincipalDep) -> StreamStatusView:
    """Each live engine: live or not, its market, the dead-man, the stream
    (connected, last event age, bars built, late ticks), dispatch lag and
    event to order latency. ``engines`` is empty until an engine runs.
    ``intraday_pnl`` says whether per-book intraday P&L is available."""
    return StreamService(services.context).status(principal)

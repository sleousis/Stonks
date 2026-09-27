from __future__ import annotations

from fastapi import APIRouter

from stonks.api.deps import PrincipalDep, ServicesDep, needs
from stonks.api.errors import PROBLEM_RESPONSES
from stonks.app.broker_gateways import GatewayHealthService, GatewayHealthView
from stonks.app.brokers import AlpacaStatus, BrokerInfo
from stonks.auth import Permission

router = APIRouter(prefix="/api/brokers", tags=["brokers"], responses=PROBLEM_RESPONSES)


@router.get("", response_model=BrokerInfo, operation_id="getBrokerInfo")
def get_broker_info(services: ServicesDep) -> BrokerInfo:
    """The broker the production tick trades through (keys never shown)."""
    return services.brokers.info()


@router.get("/alpaca/status", response_model=AlpacaStatus, operation_id="getAlpacaStatus")
def get_alpaca_status(services: ServicesDep, principal: PrincipalDep) -> AlpacaStatus:
    """Connect to Alpaca and read the account and market clock (read-only).
    Only the owner of the book the account backs may read it (404 for
    anyone else). Answers are cached for 30 seconds. 409 unless
    ``[brokers].kind`` is ``alpaca``; missing keys or API errors come back
    as ``connected=false`` with the reason."""
    return services.brokers.alpaca_status(principal)


@router.get(
    "/gateways",
    response_model=GatewayHealthView,
    operation_id="getBrokerGateways",
    dependencies=needs(Permission.READ),
)
def get_broker_gateways(services: ServicesDep, principal: PrincipalDep) -> GatewayHealthView:
    """Each IB Gateway the broker health check watches: connected or not,
    the last good check, and the auto subscriptions it paused. Portfolio
    names and paused books show for your own portfolios only."""
    return GatewayHealthService(services.context).gateways(principal)

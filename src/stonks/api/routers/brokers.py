from __future__ import annotations

from fastapi import APIRouter

from stonks.api.deps import ServicesDep
from stonks.api.errors import PROBLEM_RESPONSES
from stonks.app.brokers import AlpacaStatus, BrokerInfo

router = APIRouter(prefix="/api/brokers", tags=["brokers"], responses=PROBLEM_RESPONSES)


@router.get("", response_model=BrokerInfo, operation_id="getBrokerInfo")
def get_broker_info(services: ServicesDep) -> BrokerInfo:
    """The broker the production tick trades through (keys never shown)."""
    return services.brokers.info()


@router.get("/alpaca/status", response_model=AlpacaStatus, operation_id="getAlpacaStatus")
def get_alpaca_status(services: ServicesDep) -> AlpacaStatus:
    """Connect to Alpaca and read the account and market clock (read-only).
    409 unless ``[brokers].kind`` is ``alpaca``; missing keys or API errors
    come back as ``connected=false`` with the reason."""
    return services.brokers.alpaca_status()

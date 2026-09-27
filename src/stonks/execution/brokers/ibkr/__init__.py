"""Interactive Brokers behind the ``Broker`` seam (roadmap Phase 19).

Wave 1 (19.4) ships only the settings (``settings.py``): the gateways
Stonks can reach and how their health is judged. The adapter itself
(``IbkrBroker`` over ``ib_async``, contract resolution, order mapping,
``FakeIbGateway``) is roadmap 19.2. ``make_broker(kind="ibkr")`` refuses
until it lands.

The IBKR username and password never reach Stonks: they live in the
gateway container's Docker secret files (``deploy/ibkr/README.md``).
"""

from stonks.execution.brokers.ibkr.settings import (
    GatewayMode,
    IbkrBrokerConfig,
    IbkrClientIds,
    IbkrGatewayConfig,
    IbkrHealthSettings,
    IbkrOrderSettings,
)

__all__ = [
    "GatewayMode",
    "IbkrBrokerConfig",
    "IbkrClientIds",
    "IbkrGatewayConfig",
    "IbkrHealthSettings",
    "IbkrOrderSettings",
]

"""Interactive Brokers behind the ``Broker`` seam (roadmap Phase 19).

- ``settings.py``: the gateways Stonks can reach, how their health is
  judged and how orders are shaped (19.4, 19.2).
- ``client.py``: the ``IbClient`` protocol and its plain types.
  ``ib_async_client.py`` implements it over ``ib_async`` (the only module
  that imports it) on the session thread of ``session.py``.
- ``contracts.py``: tickers onto contracts, with the ``broker_contracts``
  conId cache. ``orders.py``: order mapping. ``errors.py``: error codes.
  ``status.py``: order statuses onto our state machine.
- ``broker.py``: ``IbkrBroker``. ``factory.py``: settings onto a broker.

Tests use ``tests/fakes/ib_gateway.py`` (``FakeIbGateway``) and never open a
socket.

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

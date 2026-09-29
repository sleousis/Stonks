"""The eToro adapter over eToro's official public API (``docs/design/etoro.md``).

``client`` signs and paces the HTTP calls, ``instruments`` maps eToro
instrument ids to our tickers, and ``broker`` is the ``Broker`` a linked
portfolio trades through. The connection provider is
``stonks.connections.providers.etoro``. Nothing here runs unless an admin
enables the ``etoro`` provider.
"""

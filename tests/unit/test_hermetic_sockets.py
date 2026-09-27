"""Default test runs cannot reach the network (TT-05).

``pytest-socket`` runs with ``--allow-hosts`` set to loopback only (see
``[tool.pytest.ini_options]``). Tests marked ``live`` get the network back.
"""

from __future__ import annotations

import socket

import pytest
from pytest_socket import SocketConnectBlockedError

from tests.conftest import enable_network_for_live_tests


def test_a_connection_to_a_public_address_is_blocked():
    with (
        pytest.raises(SocketConnectBlockedError),
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s,
    ):
        s.connect(("192.0.2.1", 443))  # TEST-NET-1, never routed


def test_loopback_is_allowed():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        with socket.create_connection(server.getsockname(), timeout=2):
            conn, _ = server.accept()
            conn.close()


class _Item:
    def __init__(self, live: bool) -> None:
        self.markers: list[pytest.Mark] = []
        self._live = live

    def get_closest_marker(self, name: str):
        return object() if name == "live" and self._live else None

    def add_marker(self, marker) -> None:
        self.markers.append(marker.mark)


def test_only_live_tests_get_the_network():
    live, hermetic = _Item(live=True), _Item(live=False)
    enable_network_for_live_tests([live, hermetic])
    assert [m.name for m in live.markers] == ["enable_socket"]
    assert hermetic.markers == []

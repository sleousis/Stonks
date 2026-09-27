"""Outbound requests to hosts a user chose (their own webhook) may only reach
public addresses (review finding AS-05, server-side request forgery).

Two checks:

- :func:`check_public_host` at save time. It refuses local names and every
  numeric host that is not global, including the shorthand forms
  ``inet_aton`` accepts (``127.1``, ``2130706433``, ``0x7f.1``, ``0177.0.0.1``).
- :func:`resolve_public` at send time. It resolves the name and refuses
  unless every address is global. The caller then connects to that exact
  address with :func:`pinned_session`, so a second DNS answer (rebinding)
  cannot move the request inside. TLS still checks the certificate against
  the name. Redirects are never followed.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

#: ``socket.getaddrinfo``-shaped resolver (injectable for tests).
Resolver = Callable[..., list[tuple[Any, ...]]]

_LOCAL_SUFFIXES = (".localhost", ".local", ".internal", ".localdomain", ".home.arpa")


class UnsafeAddress(ValueError):
    """The host is, or resolves to, an address that is not public."""


def _inet_aton_part(part: str) -> int | None:
    if not part:
        return None
    try:
        if part[:2].lower() == "0x":
            return int(part[2:], 16) if part[2:] else 0
        if len(part) > 1 and part[0] == "0":
            return int(part, 8)
        return int(part, 10) if part.isdigit() else None
    except ValueError:
        return None


def _inet_aton(host: str) -> ipaddress.IPv4Address | None:
    """The IPv4 address ``inet_aton`` (and so most HTTP stacks) reads from
    ``host``: one to four dot-separated parts, each decimal, octal (leading
    ``0``) or hex (``0x``). The last part fills the remaining bytes."""
    parts = host.split(".")
    if not 1 <= len(parts) <= 4:
        return None
    values = [_inet_aton_part(p) for p in parts]
    if any(v is None for v in values):
        return None
    nums = [v for v in values if v is not None]
    *head, last = nums
    if any(v > 255 for v in head) or last >= 256 ** (4 - len(head)):
        return None
    value = 0
    for v in head:
        value = value * 256 + v
    value = value * 256 ** (4 - len(head)) + last
    return ipaddress.IPv4Address(value)


def ip_literal(host: str) -> IPAddress | None:
    """The address ``host`` spells, in any numeric form, or ``None`` for a name."""
    host = host.strip().lower().rstrip(".")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    return _inet_aton(host)


#: The well-known NAT64 prefix: the IPv4 address sits in the low 32 bits.
_NAT64 = ipaddress.IPv6Network("64:ff9b::/96")
#: Local-use NAT64 (RFC 8215): where the IPv4 sits is up to the network,
#: so an address here is never taken as public.
_NAT64_LOCAL = ipaddress.IPv6Network("64:ff9b:1::/48")
#: Deprecated IPv4-compatible addresses (``::a.b.c.d``).
_IPV4_COMPATIBLE = ipaddress.IPv6Network("::/96")


def _unwrap(ip: IPAddress) -> IPAddress:
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return ip.ipv4_mapped
        if ip.sixtofour is not None:
            return ip.sixtofour
        if ip in _NAT64:
            return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return ip


def is_public_ip(ip: IPAddress) -> bool:
    """Public on the internet, also through every IPv6 wrapping of an IPv4
    address (mapped, 6to4, NAT64). Local-use NAT64 and IPv4-compatible
    addresses are refused outright (BE-39)."""
    if isinstance(ip, ipaddress.IPv6Address) and (ip in _NAT64_LOCAL or ip in _IPV4_COMPATIBLE):
        return False
    inner = _unwrap(ip)
    return inner.is_global and ip.is_global and not inner.is_multicast


def check_public_host(host: str) -> None:
    """Save-time check: refuse local names and non-public numeric hosts.
    A name is accepted here; :func:`resolve_public` checks it on every send."""
    name = host.strip().lower().rstrip(".")
    if not name or name == "localhost" or name.endswith(_LOCAL_SUFFIXES):
        raise UnsafeAddress(f"{host!r} is a local name")
    ip = ip_literal(name)
    if ip is not None and not is_public_ip(ip):
        raise UnsafeAddress(f"{host!r} is not a public address")


def resolve_public(host: str, port: int, *, resolver: Resolver = socket.getaddrinfo) -> str:
    """Resolve ``host`` and return one of its addresses, refusing unless
    every address it resolves to is public."""
    check_public_host(host)
    literal = ip_literal(host)
    if literal is not None:
        return str(literal)
    try:
        infos = resolver(host, port, 0, socket.SOCK_STREAM)
    except (OSError, UnicodeError) as exc:
        raise UnsafeAddress(f"{host!r} does not resolve") from exc
    addresses = []
    for info in infos:
        try:
            addresses.append(ipaddress.ip_address(str(info[4][0]).split("%", 1)[0]))
        except (IndexError, ValueError):
            continue
    if not addresses:
        raise UnsafeAddress(f"{host!r} does not resolve")
    if not all(is_public_ip(ip) for ip in addresses):
        raise UnsafeAddress(f"{host!r} resolves to an address that is not public")
    return str(addresses[0])


class _PinnedAdapter(HTTPAdapter):
    """Connects to one checked address while TLS verifies the URL's name."""

    def __init__(self, address: str) -> None:
        self._address = address
        super().__init__(max_retries=0)

    def get_connection_with_tls_context(self, request, verify, proxies=None, cert=None):
        # None means "not said": verify, the safe default.
        host_params, key_kwargs = self.build_connection_pool_key_attributes(
            request, True if verify is None else verify, cert
        )
        pool_kwargs: dict[str, Any] = dict(key_kwargs)
        name = host_params["host"]
        host_params["host"] = self._address
        if host_params["scheme"] == "https":
            pool_kwargs["server_hostname"] = name
            pool_kwargs["assert_hostname"] = name
        return self.poolmanager.connection_from_host(**host_params, pool_kwargs=pool_kwargs)

    def send(self, request, **kwargs):
        host = urlsplit(request.url).netloc.rsplit("@", 1)[-1]
        request.headers["Host"] = host
        return super().send(request, **kwargs)


def pinned_session(address: str) -> requests.Session:
    """A session that sends every request to ``address`` (already checked
    with :func:`resolve_public`), ignores proxy settings from the
    environment and never follows a redirect."""
    session = requests.Session()
    session.trust_env = False
    session.max_redirects = 0
    adapter = _PinnedAdapter(address)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session

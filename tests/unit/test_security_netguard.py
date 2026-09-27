"""AS-05: outbound URLs to user-chosen hosts only reach public addresses,
checked at save time and again, pinned, at send time."""

from __future__ import annotations

import ipaddress
import socket

import pytest
import requests

from stonks.security.netguard import (
    UnsafeAddress,
    check_public_host,
    ip_literal,
    pinned_session,
    resolve_public,
)


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("127.0.0.1", "127.0.0.1"),
        ("127.1", "127.0.0.1"),
        ("2130706433", "127.0.0.1"),
        ("0x7f000001", "127.0.0.1"),
        ("0x7f.1", "127.0.0.1"),
        ("0177.0.0.1", "127.0.0.1"),
        ("10.1", "10.0.0.1"),
        ("[::1]", "::1"),
        ("[::ffff:127.0.0.1]", "::ffff:127.0.0.1"),
        ("hooks.example.com", None),
        ("1e100.net", None),
    ],
)
def test_ip_literal_reads_every_numeric_form(host, expected):
    got = ip_literal(host)
    assert (str(got) if got is not None else None) == expected


@pytest.mark.parametrize(
    "host",
    [
        "localhost",
        "api.localhost",
        "printer.local",
        "db.internal",
        "127.1",
        "2130706433",
        "0x7f000001",
        "169.254.169.254",
        "10.0.0.5",
        "192.168.1.1",
        "[::1]",
        "[::ffff:127.0.0.1]",
        "[fe80::1]",
        "0.0.0.0",
        "100.64.0.1",
    ],
)
def test_check_public_host_refuses_private_loopback_and_link_local(host):
    with pytest.raises(UnsafeAddress):
        check_public_host(host)


@pytest.mark.parametrize("host", ["hooks.slack.com", "8.8.8.8", "[2001:4860:4860::8888]"])
def test_check_public_host_accepts_public_names_and_addresses(host):
    check_public_host(host)


def _resolver(*addresses: str):
    def resolve(host, port, *args, **kwargs):
        out = []
        for a in addresses:
            family = socket.AF_INET6 if ":" in a else socket.AF_INET
            out.append((family, socket.SOCK_STREAM, 6, "", (a, port)))
        return out

    return resolve


def test_resolve_public_returns_the_checked_address():
    assert resolve_public("hooks.example", 443, resolver=_resolver("93.184.216.34")) == (
        "93.184.216.34"
    )


def test_resolve_public_refuses_a_name_that_points_inside():
    with pytest.raises(UnsafeAddress):
        resolve_public("evil.example", 443, resolver=_resolver("10.0.0.5"))


def test_resolve_public_refuses_when_any_address_is_private():
    with pytest.raises(UnsafeAddress):
        resolve_public("mixed.example", 443, resolver=_resolver("93.184.216.34", "127.0.0.1"))


def test_resolve_public_refuses_names_that_do_not_resolve():
    def fail(*args, **kwargs):
        raise socket.gaierror("nope")

    with pytest.raises(UnsafeAddress):
        resolve_public("missing.example", 443, resolver=fail)


def test_pinned_session_connects_to_the_ip_but_verifies_the_name():
    session = pinned_session("93.184.216.34")
    request = requests.Request("POST", "https://hooks.example/T0/SECRET", json={}).prepare()
    adapter = session.get_adapter(request.url)
    pool = adapter.get_connection_with_tls_context(request, verify=True)
    assert pool.host == "93.184.216.34"
    assert pool.conn_kw.get("server_hostname") == "hooks.example"
    assert pool.assert_hostname == "hooks.example"
    assert session.max_redirects == 0
    ipaddress.ip_address(pool.host)


def test_pinned_session_verifies_certificates_when_verify_is_not_given():
    session = pinned_session("93.184.216.34")
    request = requests.Request("GET", "https://hooks.example/x").prepare()
    pool = session.get_adapter(request.url).get_connection_with_tls_context(request, verify=None)
    assert pool.cert_reqs == "CERT_REQUIRED"
    assert pool.assert_hostname == "hooks.example"


# ---- NAT64 and IPv4-compatible addresses (BE-39) ---------------------------------------


@pytest.mark.parametrize(
    "host",
    ["64:ff9b::a9fe:a9fe", "64:ff9b::a00:1", "::7f00:1", "64:ff9b:1::a9fe:a9fe", "::a9fe:a9fe"],
)
def test_nat64_and_ipv4_compatible_forms_of_private_addresses_are_refused(host):
    from stonks.security.netguard import UnsafeAddress, check_public_host, resolve_public

    with pytest.raises(UnsafeAddress):
        check_public_host(host)

    def resolver(name, port, *args):
        return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", (host, port, 0, 0))]

    with pytest.raises(UnsafeAddress):
        resolve_public("evil.example.com", 443, resolver=resolver)


def test_a_nat64_form_of_a_public_address_is_still_public():
    from stonks.security.netguard import check_public_host

    check_public_host("64:ff9b::101:101")  # 1.1.1.1 through the NAT64 prefix

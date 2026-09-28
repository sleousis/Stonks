"""The Compose stack tells the API to trust X-Forwarded-For from its own
Docker network (where Caddy sits) and from nothing else."""

from __future__ import annotations

import ipaddress
from pathlib import Path

import yaml

COMPOSE = Path(__file__).parents[2] / "deploy" / "compose.yaml"


def _default(value: str) -> str:
    """``${VAR:-default}`` -> ``default``."""
    return value.split(":-", 1)[1].rstrip("}") if ":-" in value else value


def test_api_trusts_forwarded_headers_from_the_compose_network_only():
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    subnet = _default(compose["networks"]["default"]["ipam"]["config"][0]["subnet"])
    trusted = _default(compose["services"]["api"]["environment"]["STONKS_API_TRUSTED_PROXIES"])
    assert trusted == subnet
    assert ipaddress.ip_network(subnet).is_private
    # The API port is only reachable inside that network, never published.
    assert "ports" not in compose["services"]["api"]


def test_caddy_passes_the_real_host_and_the_api_allows_only_the_domain():
    """Caddy no longer rewrites Host to localhost: the API's trusted-host
    check sees the real name and accepts STONKS_DOMAIN plus ``api``."""
    caddyfile = (COMPOSE.parent / "Caddyfile").read_text(encoding="utf-8")
    assert "header_up Host" not in caddyfile
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    env = compose["services"]["api"]["environment"]
    assert env["STONKS_API_ALLOWED_HOSTS"] == "${STONKS_DOMAIN:-localhost},api"
    assert env["STONKS_API_HOST"] == "0.0.0.0"
    assert "--host" not in compose["services"]["api"]["command"]
    assert compose["services"]["caddy"]["environment"]["STONKS_DOMAIN"] == (
        "${STONKS_DOMAIN:-localhost}"
    )

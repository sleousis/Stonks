"""IB Gateway and model server Compose services (roadmap 19.4, 20.6)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[2]
DEPLOY = ROOT / "deploy"
COMPOSE = DEPLOY / "compose.yaml"

GATEWAYS = {
    "ib-gateway-paper": ("ibkr-paper", "paper", "4004"),
    "ib-gateway-live": ("ibkr-live", "live", "4003"),
}
CORE = ("api", "caddy")
STONKS_APPS = ("api", "scheduler", "lab-worker")


def _default(value: object) -> str:
    """``${VAR:-default}`` -> ``default``."""
    text = str(value)
    return text.split(":-", 1)[1].rstrip("}") if ":-" in text else text


def _compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def _networks(service: dict) -> set[str]:
    """The networks a service joins (Compose puts it on `default` when none are named)."""
    nets = service.get("networks")
    if nets is None:
        return {"default"}
    return set(nets)  # a list of names, or a mapping keyed by name


def _secret_names(service: dict) -> set[str]:
    return {s if isinstance(s, str) else s["source"] for s in service.get("secrets", [])}


@pytest.mark.parametrize("name", sorted(GATEWAYS))
def test_each_gateway_sits_behind_its_own_profile(name):
    profile, mode, port = GATEWAYS[name]
    gateway = _compose()["services"][name]
    assert gateway["profiles"] == [profile]
    assert gateway["image"].startswith("ghcr.io/gnzsnz/ib-gateway:")
    tag = gateway["image"].rsplit(":", 1)[1]
    assert tag not in {"latest", "stable"}, "pin the gateway image to a version"
    env = gateway["environment"]
    assert env["TRADING_MODE"] == mode
    assert port in [str(p) for p in gateway["expose"]]
    assert gateway["restart"] == "unless-stopped"
    assert _default(gateway["mem_limit"]).endswith(("g", "m"))


@pytest.mark.parametrize("name", sorted(GATEWAYS))
def test_gateways_log_in_again_by_themselves(name):
    env = _compose()["services"][name]["environment"]
    assert _default(env["READ_ONLY_API"]) == "no"
    assert env["TWS_ACCEPT_INCOMING"] == "accept"
    assert env["TWOFA_TIMEOUT_ACTION"] == "restart"
    assert env["RELOGIN_AFTER_TWOFA_TIMEOUT"] == "yes"
    assert _default(env["TIME_ZONE"]) == "America/New_York"
    assert _default(env["AUTO_RESTART_TIME"])


@pytest.mark.parametrize("name", sorted(GATEWAYS))
def test_gateways_publish_nothing_and_check_their_port(name):
    gateway = _compose()["services"][name]
    assert "ports" not in gateway
    _, _, port = GATEWAYS[name]
    check = " ".join(gateway["healthcheck"]["test"])
    assert f"/dev/tcp/127.0.0.1/{port}" in check
    # VNC stays off: no VNC password means the image starts no VNC server.
    env = gateway["environment"]
    assert not any(key.startswith("VNC_SERVER_PASSWORD") for key in env)


def test_the_ibkr_network_is_internal_and_only_the_stack_that_trades_joins_it():
    compose = _compose()
    assert compose["networks"]["ibkr"]["internal"] is True
    members = {name for name, svc in compose["services"].items() if "ibkr" in _networks(svc)}
    assert members == {"api", "scheduler", *GATEWAYS}
    # api and scheduler keep the default network (Caddy, the proxy trust range).
    for name in ("api", "scheduler"):
        assert "default" in _networks(compose["services"][name])


def test_gateways_reach_ibkr_through_their_own_egress_network():
    compose = _compose()
    for name in GATEWAYS:
        nets = _networks(compose["services"][name])
        assert "default" not in nets
        assert "ibkr" in nets
    egress = {name for name, svc in compose["services"].items() if "ibkr_egress" in _networks(svc)}
    assert egress == set(GATEWAYS)
    assert "ibkr_egress" in compose["networks"]
    assert not (compose["networks"]["ibkr_egress"] or {}).get("internal", False)


@pytest.mark.parametrize("name", sorted(GATEWAYS))
def test_gateway_credentials_come_only_from_secret_files(name):
    compose = _compose()
    gateway = compose["services"][name]
    env = gateway["environment"]
    for key in ("TWS_PASSWORD", "TWS_USERID", "TWS_PASSWORD_PAPER", "TWS_USERID_PAPER"):
        assert key not in env, f"{key} would put a credential in plain text"
    assert env["TWS_PASSWORD_FILE"].startswith("/run/secrets/")
    names = _secret_names(gateway)
    assert Path(env["TWS_PASSWORD_FILE"]).name in names
    # The image has no TWS_USERID_FILE, so the entrypoint reads the user id file.
    user_file = env["STONKS_TWS_USERID_FILE"]
    assert user_file.startswith("/run/secrets/")
    assert Path(user_file).name in names
    assert "STONKS_TWS_USERID_FILE" in " ".join(gateway["entrypoint"])
    _, mode, _ = GATEWAYS[name]
    for secret in names:
        assert mode in secret
        source = compose["secrets"][secret]["file"]
        assert source.startswith("./ibkr/secrets/")


def test_paper_and_live_never_share_a_secret_file():
    compose = _compose()
    paper = _secret_names(compose["services"]["ib-gateway-paper"])
    live = _secret_names(compose["services"]["ib-gateway-live"])
    assert paper and live and not paper & live
    files = [compose["secrets"][s]["file"] for s in paper | live]
    assert len(files) == len(set(files))


def test_stonks_containers_never_mount_gateway_secrets():
    services = _compose()["services"]
    for name in STONKS_APPS:
        svc = services[name]
        assert "secrets" not in svc
        assert not any("ibkr/secrets" in str(v) for v in svc.get("volumes", []))


def test_no_service_carries_a_plaintext_ibkr_password():
    for name, svc in _compose()["services"].items():
        env = svc.get("environment") or {}
        keys = env if isinstance(env, dict) else [e.split("=", 1)[0] for e in env]
        for key in keys:
            assert not (("PASSWORD" in key) and not key.endswith("_FILE")), (name, key)


def test_the_model_server_is_optional_private_and_on_the_default_network():
    server = _compose()["services"]["model-server"]
    assert server["profiles"] == ["ai"]
    assert server["image"].startswith("ollama/ollama:")
    assert server["image"].rsplit(":", 1)[1] != "latest"
    assert "ports" not in server
    assert _networks(server) == {"default"}
    assert any(str(v).startswith("models:") for v in server["volumes"])
    assert "models" in _compose()["volumes"]


def test_core_services_have_no_profile_and_optional_ones_do():
    services = _compose()["services"]
    for name in CORE:
        assert "profiles" not in services[name], name
    for name, svc in services.items():
        if name not in CORE:
            assert svc.get("profiles"), f"{name} would start on a plain `up`"


def test_the_gateway_secret_folder_is_ignored_by_git():
    ignore = DEPLOY / "ibkr" / "secrets" / ".gitignore"
    lines = [line.strip() for line in ignore.read_text(encoding="utf-8").splitlines()]
    rules = [line for line in lines if line and not line.startswith("#")]
    assert rules[0] == "*"
    assert all(rule.startswith("!") for rule in rules[1:])
    assert "!.gitignore" in rules
    root_ignore = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "deploy/ibkr/secrets/*" in root_ignore
    assert "!deploy/ibkr/secrets/.gitignore" in root_ignore

"""Surface parity (roadmap 18.7): every API capability is reachable from the
console and MCP, or is written down as deliberately limited.

The matrix lives in tests/parity/capabilities.toml. The test fails when:
- an OpenAPI operation is not in the matrix (a new route with no decision),
- a capability with routes is reachable from neither the console nor MCP,
  unless both say why (``limited``),
- a claimed entry does not exist (operationId, CLI command, MCP tool),
- a tracked gap has in fact been filled (the entry must replace it),
- the number of tracked gaps grew,
- an MCP tool or a CLI command appears in no capability.
Hermetic: no server, no network.
"""

from __future__ import annotations

import json
import re
import tomllib
from functools import cache
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
MATRIX = ROOT / "tests" / "parity" / "capabilities.toml"
OPENAPI = ROOT / "web" / "openapi.json"
CONSOLE = ROOT / "web" / "src" / "app"
METHODS = {"get", "post", "put", "patch", "delete"}
SURFACES = ("console", "cli", "mcp")
MIN_REASON = 12  # "operator only" is fine, "n/a" alone is not

#: Tracked gaps allowed. A ratchet like the pyright and coverage baselines:
#: filling a gap lowers it, and adding one needs a reviewer to raise it.
#: Raised for the roadmap 20.7 and 20.8 console pages (calendars, news, the
#: order ticket's earnings warning, event alert toggles, the screener).
MAX_TRACKED_GAPS = 4

#: The argparse scheduler mounted under Typer shows up as one leaf.
ARGPARSE_LEAVES = {"stonks schedule": ROOT / "src" / "stonks" / "scheduling" / "__main__.py"}


@cache
def matrix() -> dict[str, Any]:
    return tomllib.loads(MATRIX.read_text(encoding="utf-8"))


def capabilities() -> list[dict[str, Any]]:
    return matrix()["capability"]


@cache
def api_operations() -> dict[str, str]:
    """``"METHOD /path"`` -> operationId."""
    spec = json.loads(OPENAPI.read_text(encoding="utf-8"))
    return {
        f"{method.upper()} {path}": op["operationId"]
        for path, ops in spec["paths"].items()
        for method, op in ops.items()
        if method in METHODS
    }


@cache
def console_sources() -> str:
    """Every hand-written console file (generated client and specs excluded)."""
    parts = []
    for f in CONSOLE.rglob("*.ts"):
        if "generated" in f.parts or f.name.endswith(".spec.ts"):
            continue
        parts.append(f.read_text(encoding="utf-8"))
    return "\n".join(parts)


@cache
def mcp_tools() -> frozenset[str]:
    """Every registered MCP tool, old alias names included."""
    from stonks.mcp.docs import collect_reference

    return frozenset(t["name"] for t in collect_reference()["tools"])


@cache
def mcp_aliases() -> frozenset[str]:
    from stonks.mcp.tools.common import ALIASES

    return frozenset(ALIASES)


@cache
def cli_commands() -> frozenset[str]:
    """Every leaf Typer command as ``stonks group sub``."""
    import click
    import typer

    from stonks.cli import app

    out: set[str] = set()

    def walk(cmd: click.Command, prefix: str) -> None:
        if isinstance(cmd, click.Group):
            for name, sub in cmd.commands.items():
                walk(sub, f"{prefix} {name}")
        else:
            out.add(prefix)

    walk(typer.main.get_command(app), "stonks")
    return frozenset(out)


def kind(value: Any) -> str:
    if isinstance(value, (str, list)):
        return "entry"
    if isinstance(value, dict) and "limited" in value:
        return "limited"
    if isinstance(value, dict) and "gap" in value:
        return "gap"
    raise AssertionError(f"bad surface value: {value!r}")


def ids(cap: dict[str, Any]) -> str:
    return cap["id"]


def _entries(value: str | list[str]) -> list[str]:
    return [value] if isinstance(value, str) else list(value)


def _argparse_leaf(entry: str) -> str | None:
    for leaf in ARGPARSE_LEAVES:
        if entry.startswith(f"{leaf} "):
            return leaf
    return None


# ---- the matrix itself -------------------------------------------------------------


def test_matrix_shape():
    assert matrix()["schema_version"] == 1
    seen: set[str] = set()
    for cap in capabilities():
        assert cap["id"] not in seen, f"duplicate id {cap['id']}"
        seen.add(cap["id"])
        assert {"id", "domain", "title", "routes", *SURFACES} <= cap.keys(), cap["id"]
        for surface in SURFACES:
            value = cap[surface]
            if kind(value) == "limited":
                assert len(value["limited"]) >= MIN_REASON, f"{cap['id']}.{surface}: say why"
            if kind(value) == "entry":
                assert _entries(value), f"{cap['id']}.{surface}: empty entry list"


def test_every_api_route_is_in_the_matrix_once():
    ops = api_operations()
    claimed: dict[str, str] = {}
    for cap in capabilities():
        for route in cap["routes"]:
            assert route in ops, f"{cap['id']}: {route} is not in openapi.json (removed?)"
            assert route not in claimed, f"{route} in both {claimed[route]} and {cap['id']}"
            claimed[route] = cap["id"]
    infra = matrix().get("infra_routes", {})
    missing = sorted(set(ops) - set(claimed) - set(infra))
    assert not missing, (
        "new API routes with no parity decision; add them to tests/parity/capabilities.toml:\n"
        + "\n".join(missing)
    )
    stale = sorted(set(infra) - set(ops))
    assert not stale, f"infra_routes no longer in openapi.json: {stale}"
    assert not set(infra) & set(claimed), "a route is both infra and a capability"


# ---- the rule ------------------------------------------------------------------------


@pytest.mark.parametrize("cap", [c for c in capabilities() if c["routes"]], ids=ids)
def test_api_capability_reaches_console_or_mcp(cap):
    """A route-backed capability is reachable from the console or MCP, or
    both surfaces say why not."""
    console, mcp = kind(cap["console"]), kind(cap["mcp"])
    assert console == "entry" or mcp == "entry" or (console == mcp == "limited"), (
        f"{cap['id']}: reachable from neither the console nor MCP, and not limited on both"
    )


def test_tracked_gaps_only_shrink():
    gaps = [(c["id"], s) for c in capabilities() for s in SURFACES if kind(c[s]) == "gap"]
    assert len(gaps) <= MAX_TRACKED_GAPS, f"new tracked gaps: {gaps}"


# ---- claims are true -------------------------------------------------------------------


@pytest.mark.parametrize("cap", capabilities(), ids=ids)
def test_console_claims_are_true(cap):
    value = cap["console"]
    src = console_sources()
    if kind(value) == "entry":
        for op_id in _entries(value):
            assert op_id in api_operations().values(), f"{cap['id']}: unknown operationId {op_id}"
            assert re.search(rf"\b{op_id}\b", src), f"{cap['id']}: console never imports {op_id}"
    if kind(value) == "gap":
        for route in cap["routes"]:
            op_id = api_operations()[route]
            assert not re.search(rf"\b{op_id}\b", src), (
                f"{cap['id']}: the console now calls {op_id}; replace the gap with the entry"
            )


@pytest.mark.parametrize("cap", capabilities(), ids=ids)
def test_mcp_claims_are_true(cap):
    value = cap["mcp"]
    if kind(value) == "entry":
        for tool in _entries(value):
            assert tool in mcp_tools(), f"{cap['id']}: no MCP tool {tool}"
            assert tool not in mcp_aliases(), f"{cap['id']}: name the tool, not its old alias"


@pytest.mark.parametrize("cap", capabilities(), ids=ids)
def test_cli_claims_are_true(cap):
    value = cap["cli"]
    if kind(value) != "entry":
        return
    for entry in _entries(value):
        leaf = _argparse_leaf(entry)
        if leaf is not None:
            sub = entry.split()[len(leaf.split())]
            src = ARGPARSE_LEAVES[leaf].read_text(encoding="utf-8")
            assert f'"{sub}"' in src, f"{cap['id']}: no subcommand {sub} under {leaf}"
        elif entry.startswith("stonks "):
            assert entry in cli_commands(), f"{cap['id']}: no CLI command '{entry}'"
        elif entry.startswith("python -m stonks."):
            module, *sub = entry.removeprefix("python -m ").split()
            path = ROOT / "src" / Path(*module.split(".")) / "__main__.py"
            if not path.exists():
                path = ROOT / "src" / Path(*module.split(".")).with_suffix(".py")
            assert path.exists(), f"{cap['id']}: no module {module}"
            if sub:
                assert f'"{sub[0]}"' in path.read_text(encoding="utf-8"), (
                    f"{cap['id']}: {module} has no subcommand {sub[0]}"
                )
        else:
            raise AssertionError(f"{cap['id']}: CLI entry must start with 'stonks ' or 'python -m'")


# ---- nothing unlisted --------------------------------------------------------------------


def test_every_mcp_tool_is_in_the_matrix():
    listed = {t for c in capabilities() if kind(c["mcp"]) == "entry" for t in _entries(c["mcp"])}
    orphans = sorted(mcp_tools() - listed - mcp_aliases())
    assert not orphans, f"MCP tools in no capability: {orphans}"


def test_every_cli_command_is_in_the_matrix():
    listed: set[str] = set()
    for cap in capabilities():
        if kind(cap["cli"]) == "entry":
            for entry in _entries(cap["cli"]):
                listed.add(_argparse_leaf(entry) or entry)
    orphans = sorted(cli_commands() - listed)
    assert not orphans, f"CLI commands in no capability: {orphans}"

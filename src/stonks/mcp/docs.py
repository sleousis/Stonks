"""Generate the MCP tool reference from the live server definition.

Regenerate after adding or changing a tool::

    uv run python -m stonks.mcp.docs            # writes docs/api/mcp-tools.{json,md}
    uv run python -m stonks.mcp.docs out/dir    # custom directory

The server is built in-process and read through the SDK's public
``list_tools`` / ``list_resources``; nothing is served and the API is never
contacted. ``tests/unit/test_mcp_docs.py`` fails when the checked-in files
are stale.
"""

from __future__ import annotations

import inspect
import json
import sys
from pathlib import Path
from typing import Any

import anyio

from stonks.mcp.client import ApiClient
from stonks.mcp.server import build_server

DEFAULT_OUT_DIR = Path("docs/api")
JSON_NAME = "mcp-tools.json"
MD_NAME = "mcp-tools.md"

# Never contacted: the client only exists so the tools can be registered.
_PLACEHOLDER_API = "http://127.0.0.1:8000"

# MCP spec defaults for a tool that declares no annotations.
_DEFAULT_HINTS = {"read_only": False, "destructive": True, "idempotent": False, "open_world": True}
_HINT_FIELDS = {
    "read_only": "read_only_hint",
    "destructive": "destructive_hint",
    "idempotent": "idempotent_hint",
    "open_world": "open_world_hint",
}


# --- collect ------------------------------------------------------------------


def collect_reference() -> dict[str, Any]:
    """Tools, resources and server instructions as plain, sorted data."""
    return anyio.run(_collect)


async def _collect() -> dict[str, Any]:
    api = ApiClient(_PLACEHOLDER_API)
    try:
        server = build_server(api)
        tools = await server.list_tools()
        resources = await server.list_resources()
        templates = await server.list_resource_templates()
    finally:
        await api.aclose()
    return {
        "server": server.name,
        "instructions": server.instructions or "",
        "tools": sorted((_tool(t) for t in tools), key=lambda t: t["name"]),
        "resources": sorted(
            (
                {
                    "uri": str(r.uri),
                    "name": r.name,
                    "mime_type": r.mime_type,
                    "description": inspect.cleandoc(r.description or ""),
                }
                for r in resources
            ),
            key=lambda r: r["uri"],
        ),
        "resource_templates": sorted(
            (
                {
                    "uri_template": t.uri_template,
                    "name": t.name,
                    "mime_type": t.mime_type,
                    "description": inspect.cleandoc(t.description or ""),
                }
                for t in templates
            ),
            key=lambda t: t["uri_template"],
        ),
    }


def _tool(tool: Any) -> dict[str, Any]:
    ann = tool.annotations
    hints = {}
    for key, attr in _HINT_FIELDS.items():
        value = getattr(ann, attr, None) if ann is not None else None
        hints[key] = _DEFAULT_HINTS[key] if value is None else bool(value)
    schema = tool.input_schema
    return {
        "name": tool.name,
        "description": inspect.cleandoc(tool.description or ""),
        "annotations": hints,
        "needs_confirm": "confirm" in schema.get("properties", {}),
        "input_schema": schema,
    }


# --- render -------------------------------------------------------------------


def render_json(ref: dict[str, Any] | None = None) -> str:
    ref = collect_reference() if ref is None else ref
    return json.dumps(ref, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _kind(tool: dict[str, Any]) -> str:
    if tool["annotations"]["read_only"]:
        return "read"
    return "guarded" if tool["needs_confirm"] else "job"


_GROUPS = (
    ("read", "Read tools", "Only issue GETs. Safe to call any time."),
    ("job", "Job tools", "Queue background work on the API. Research data only, never orders."),
    (
        "guarded",
        "Guarded tools",
        "Need `confirm=true` to act. Without it they return a preview and change nothing.",
    ),
)


def _cell(text: str) -> str:
    return " ".join(str(text).split()).replace("|", "\\|")


def _type(schema: dict[str, Any]) -> str:
    if "anyOf" in schema:
        return " \\| ".join(_type(s) for s in schema["anyOf"])
    if "enum" in schema:
        return " \\| ".join(json.dumps(v) for v in schema["enum"])
    kind = schema.get("type", "any")
    if kind == "array":
        return f"list[{_type(schema.get('items', {}))}]"
    if kind == "string" and "format" in schema:
        return schema["format"]
    return kind


def _description(schema: dict[str, Any]) -> str:
    if "description" in schema:
        return schema["description"]
    for option in schema.get("anyOf", []):
        if "description" in option:
            return option["description"]
    return ""


def _default(schema: dict[str, Any]) -> str:
    return f"`{json.dumps(schema['default'])}`" if "default" in schema else ""


def _safety(hints: dict[str, bool]) -> str:
    parts = [
        "read-only" if hints["read_only"] else "writes",
        "destructive" if hints["destructive"] else "non-destructive",
        "idempotent" if hints["idempotent"] else "not idempotent",
        "open world" if hints["open_world"] else "closed world",
    ]
    return ", ".join(parts)


def _render_tool(tool: dict[str, Any]) -> list[str]:
    lines = [f"### `{tool['name']}`", "", tool["description"], ""]
    confirm = "**yes**" if tool["needs_confirm"] else "no"
    lines += [f"Safety: {_safety(tool['annotations'])}. Needs confirm: {confirm}.", ""]
    props = tool["input_schema"].get("properties", {})
    if not props:
        return [*lines, "No inputs.", ""]
    required = set(tool["input_schema"].get("required", []))
    lines += [
        "| Input | Type | Required | Default | Description |",
        "|-------|------|----------|---------|-------------|",
    ]
    for name, prop in props.items():
        req = "yes" if name in required else "no"
        lines.append(
            f"| `{name}` | {_type(prop)} | {req} | {_default(prop)} | {_cell(_description(prop))} |"
        )
    return [*lines, ""]


def render_markdown(ref: dict[str, Any] | None = None) -> str:
    ref = collect_reference() if ref is None else ref
    tools = ref["tools"]
    out = [
        "# MCP tools",
        "",
        "<!-- Generated by `uv run python -m stonks.mcp.docs`. Do not edit. -->",
        "",
        f"Tools exposed by `stonks mcp` (server `{ref['server']}`). Setup and the",
        "safety model: [docs/mcp.md](../mcp.md).",
        "",
        "| Tool | Kind | Needs confirm |",
        "|------|------|---------------|",
    ]
    for tool in tools:
        confirm = "yes" if tool["needs_confirm"] else "no"
        out.append(f"| [`{tool['name']}`](#{tool['name']}) | {_kind(tool)} | {confirm} |")
    out.append("")
    for kind, title, blurb in _GROUPS:
        group = [t for t in tools if _kind(t) == kind]
        if not group:
            continue
        out += [f"## {title}", "", blurb, ""]
        for tool in group:
            out += _render_tool(tool)
    if ref["resources"] or ref["resource_templates"]:
        out += ["## Resources", "", "| URI | Name | Type | Description |"]
        out.append("|-----|------|------|-------------|")
        for r in ref["resources"]:
            out.append(
                f"| `{r['uri']}` | {r['name']} | {r['mime_type'] or ''} | "
                f"{_cell(r['description'])} |"
            )
        for t in ref["resource_templates"]:
            out.append(
                f"| `{t['uri_template']}` | {t['name']} | {t['mime_type'] or ''} | "
                f"{_cell(t['description'])} |"
            )
        out.append("")
    return "\n".join(out).rstrip("\n") + "\n"


# --- write --------------------------------------------------------------------


def write_docs(out_dir: Path = DEFAULT_OUT_DIR) -> list[Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ref = collect_reference()
    written = []
    for name, text in ((JSON_NAME, render_json(ref)), (MD_NAME, render_markdown(ref))):
        path = out_dir / name
        path.write_text(text, encoding="utf-8", newline="\n")
        written.append(path)
    return written


def main(argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    for path in write_docs(Path(args[0]) if args else DEFAULT_OUT_DIR):
        print(f"wrote {path.as_posix()}")


if __name__ == "__main__":
    main()

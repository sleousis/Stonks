"""MCP tool reference generator (``stonks.mcp.docs``).

The checked-in ``docs/api/mcp-tools.{json,md}`` must match the server; if
the staleness test fails, run ``uv run python -m stonks.mcp.docs`` and commit.
"""

from __future__ import annotations

import json
from pathlib import Path

from stonks.mcp.docs import (
    DEFAULT_OUT_DIR,
    collect_reference,
    render_json,
    render_markdown,
    write_docs,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
HINTS = {"read_only", "destructive", "idempotent", "open_world"}


def test_reference_lists_every_tool_sorted_with_schema_and_hints():
    ref = collect_reference()
    names = [t["name"] for t in ref["tools"]]
    assert names == sorted(names)
    assert {"health", "get_portfolio", "run_tick", "promote_strategy"} <= set(names)
    for tool in ref["tools"]:
        assert tool["description"]
        assert tool["input_schema"]["type"] == "object"
        assert set(tool["annotations"]) == HINTS


def test_confirm_flag_marks_guarded_tools_only():
    tools = {t["name"]: t for t in collect_reference()["tools"]}
    assert tools["run_tick"]["needs_confirm"] is True
    assert tools["retire_strategy"]["needs_confirm"] is True
    assert tools["health"]["needs_confirm"] is False
    assert tools["run_backtest"]["needs_confirm"] is False


def test_reference_lists_resources():
    uris = [r["uri"] for r in collect_reference()["resources"]]
    assert "stonks://portfolio/summary" in uris


def test_descriptions_are_dedented():
    for tool in collect_reference()["tools"]:
        assert "\n    " not in tool["description"]


def test_render_is_deterministic():
    assert render_json() == render_json()
    assert render_markdown() == render_markdown()


def test_markdown_has_section_inputs_and_hints_per_tool():
    md = render_markdown()
    assert md.startswith("# MCP tools")
    assert "### `run_tick`" in md
    assert "| `dry_run` | boolean | no | `true` |" in md
    assert "Needs confirm: **yes**" in md


def test_write_docs(tmp_path):
    paths = write_docs(tmp_path)
    assert sorted(p.name for p in paths) == ["mcp-tools.json", "mcp-tools.md"]
    data = json.loads((tmp_path / "mcp-tools.json").read_text(encoding="utf-8"))
    assert data["server"] == "stonks"


def test_no_local_paths_or_tokens():
    text = render_json() + render_markdown()
    assert str(REPO_ROOT) not in text
    assert "Bearer " not in text


def test_checked_in_reference_is_current():
    for name, render in (("mcp-tools.json", render_json), ("mcp-tools.md", render_markdown)):
        path = REPO_ROOT / DEFAULT_OUT_DIR / name
        assert path.is_file(), f"{name} missing: run `uv run python -m stonks.mcp.docs`"
        assert path.read_text(encoding="utf-8") == render(), (
            f"docs/api/{name} is stale: run `uv run python -m stonks.mcp.docs` and commit it"
        )

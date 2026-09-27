"""The MCP ``run_ingest`` tool offers what ``POST /api/ingest/runs`` takes:
every ingest kind and every request field (roadmap 17.9)."""

from __future__ import annotations

from typing import Any, get_args

from stonks.app.ingest import IngestKind, IngestRequest, SourceId
from stonks.mcp.docs import collect_reference


def _schema() -> dict[str, Any]:
    tools = {t["name"]: t for t in collect_reference()["tools"]}
    return tools["run_ingest"]["input_schema"]


def _enum(prop: dict[str, Any]) -> set[str]:
    if "enum" in prop:
        return set(prop["enum"])
    return {v for option in prop.get("anyOf", []) for v in option.get("enum", [])}


def test_run_ingest_lists_every_kind_the_api_accepts():
    assert _enum(_schema()["properties"]["kind"]) == set(get_args(IngestKind))


def test_run_ingest_takes_every_request_field():
    assert set(_schema()["properties"]) == set(IngestRequest.model_fields)


def test_run_ingest_names_the_same_sources():
    assert _enum(_schema()["properties"]["source"]) == set(get_args(SourceId))

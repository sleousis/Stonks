"""REST reference generator (``stonks.api.docs``).

``docs/api/rest.md`` is rendered from ``web/openapi.json``; if the staleness
test fails, run ``uv run python -m stonks.api.docs`` and commit.
"""

from __future__ import annotations

import json
from pathlib import Path

from stonks.api.docs import DEFAULT_OUTPUT, DEFAULT_SPEC, render_rest_markdown, write_rest

REPO_ROOT = Path(__file__).resolve().parents[2]

REF = "#/components/schemas/"
SPEC = {
    "openapi": "3.1.0",
    "info": {"title": "Demo API", "version": "1.2.3", "description": "Demo."},
    "components": {
        "schemas": {
            "Thing": {
                "type": "object",
                "description": "A thing.\n\nMore detail.",
                "required": ["id"],
                "properties": {
                    "id": {"type": "string", "description": "thing id"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "parent": {"anyOf": [{"$ref": REF + "Thing"}, {"type": "null"}]},
                    "kind": {"enum": ["a", "b"], "type": "string"},
                },
            },
            "NewThing": {"type": "object", "properties": {"name": {"type": "string"}}},
        }
    },
    "paths": {
        "/api/health": {
            "get": {
                "tags": ["health"],
                "summary": "Liveness probe",
                "responses": {"200": {"description": "ok"}},
            }
        },
        "/api/things": {
            "get": {
                "tags": ["things"],
                "summary": "List Things",
                "security": [{"HTTPBearer": []}],
                "responses": {
                    "200": {
                        "description": "ok",
                        "content": {
                            "application/json": {
                                "schema": {"type": "array", "items": {"$ref": REF + "Thing"}}
                            }
                        },
                    }
                },
            },
            "post": {
                "tags": ["things"],
                "summary": "Create Thing",
                "security": [{"HTTPBearer": []}],
                "requestBody": {
                    "content": {"application/json": {"schema": {"$ref": REF + "NewThing"}}}
                },
                "responses": {
                    "201": {
                        "description": "ok",
                        "content": {"application/json": {"schema": {"$ref": REF + "Thing"}}},
                    }
                },
            },
        },
    },
}


def test_groups_endpoints_by_tag_with_auth_and_models():
    md = render_rest_markdown(SPEC)
    assert md.startswith("# REST API")
    assert "```mermaid" in md
    assert "## health" in md and "## things" in md
    assert md.index("## health") < md.index("## things")
    # Tag anchors must not collide with same-named schemas (e.g. Health).
    assert "[things](#things-endpoints)" in md
    lines = md.splitlines()
    get_row = next(ln for ln in lines if ln.startswith("| GET | `/api/things`"))
    assert "token, or open on loopback" in get_row
    assert "list[[Thing](#thing)]" in get_row
    post_row = next(ln for ln in lines if ln.startswith("| POST | `/api/things`"))
    assert "bearer token" in post_row
    assert "[NewThing](#newthing)" in post_row
    health_row = next(ln for ln in lines if ln.startswith("| GET | `/api/health`"))
    assert "| none |" in health_row


def test_schema_section_lists_fields():
    md = render_rest_markdown(SPEC)
    assert "### Thing" in md
    assert "A thing." in md and "More detail." not in md
    assert "| `id` | string | yes | thing id |" in md
    assert "| `parent` | [Thing](#thing) \\| null | no |  |" in md
    assert '| `kind` | "a" \\| "b" | no |  |' in md


def test_render_is_deterministic():
    assert render_rest_markdown(SPEC) == render_rest_markdown(SPEC)


def test_write_rest(tmp_path):
    spec = tmp_path / "openapi.json"
    spec.write_text(json.dumps(SPEC), encoding="utf-8")
    out = write_rest(spec, tmp_path / "api" / "rest.md")
    assert out.read_text(encoding="utf-8").startswith("# REST API")


def test_checked_in_rest_reference_is_current():
    spec = json.loads((REPO_ROOT / DEFAULT_SPEC).read_text(encoding="utf-8"))
    path = REPO_ROOT / DEFAULT_OUTPUT
    assert path.is_file(), "docs/api/rest.md missing: run `uv run python -m stonks.api.docs`"
    assert path.read_text(encoding="utf-8") == render_rest_markdown(spec), (
        "docs/api/rest.md is stale: run `uv run python -m stonks.api.docs` and commit it"
    )

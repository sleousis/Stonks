"""The checked-in OpenAPI contract (``web/openapi.json``) must match the app.

The Angular client is generated from that file; if this fails, regenerate
it with ``uv run python -m stonks.api.openapi`` and commit the result.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from stonks.api.openapi import DEFAULT_OUTPUT, render_openapi, write_openapi

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_render_is_deterministic():
    assert render_openapi() == render_openapi()


def test_write_openapi(tmp_path):
    out = write_openapi(tmp_path / "web" / "openapi.json")
    assert json.loads(out.read_text(encoding="utf-8"))["info"]["title"] == "Stonks API"


_PINNED = tuple(
    int(x)
    for x in (Path(__file__).parents[3] / ".python-version").read_text().strip().split(".")[:2]
)


@pytest.mark.skipif(
    sys.version_info[:2] != _PINNED,
    reason="the checked-in spec is generated on the pinned Python; schema details differ by version",
)
def test_checked_in_openapi_is_current():
    path = REPO_ROOT / DEFAULT_OUTPUT
    assert path.is_file(), "web/openapi.json missing: run `uv run python -m stonks.api.openapi`"
    checked_in = json.loads(path.read_text(encoding="utf-8"))
    assert checked_in == json.loads(render_openapi()), (
        "web/openapi.json is stale: run `uv run python -m stonks.api.openapi` and commit it"
    )

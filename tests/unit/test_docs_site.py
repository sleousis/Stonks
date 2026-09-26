"""Static docs site (``docs/site``) and its Pages workflow stay wired up."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SITE = REPO_ROOT / "docs" / "site"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "docs.yml"
CDN = re.compile(r'(?:src|href)="(https://[^"]+)"')


@pytest.mark.parametrize("page", ["index.html", "rest.html", "redoc.html", "mcp.html"])
def test_pages_exist_and_are_mobile_ready(page):
    html = (SITE / page).read_text(encoding="utf-8")
    assert '<meta name="viewport"' in html


def test_cdn_assets_are_pinned_with_integrity():
    for page in SITE.glob("*.html"):
        html = page.read_text(encoding="utf-8")
        for url in CDN.findall(html):
            if "cdn.jsdelivr.net" not in url:
                continue
            assert re.search(r"@\d+\.\d+\.\d+/", url), f"{page.name}: unpinned {url}"
            tag = html[html.rindex("<", 0, html.index(url)) : html.index(">", html.index(url))]
            assert "integrity=" in tag, f"{page.name}: no SRI for {url}"


def test_pages_load_the_published_json_files():
    assert 'url: "./openapi.json"' in (SITE / "rest.html").read_text(encoding="utf-8")
    assert 'fetch("./mcp-tools.json")' in (SITE / "mcp.html").read_text(encoding="utf-8")


def test_mcp_page_follows_color_scheme():
    assert "prefers-color-scheme: dark" in (SITE / "style.css").read_text(encoding="utf-8")


def test_workflow_regenerates_checks_and_deploys():
    yaml = pytest.importorskip("yaml")
    wf = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    triggers = wf[True]  # YAML 1.1 reads the bare key `on` as True
    assert triggers["push"]["branches"] == ["main"] and "workflow_dispatch" in triggers
    assert wf["concurrency"]["group"] == "pages"
    build = "\n".join(s.get("run", "") for s in wf["jobs"]["build"]["steps"])
    for module in ("stonks.api.openapi", "stonks.api.docs", "stonks.mcp.docs"):
        assert f"uv run python -m {module}" in build
    assert "uv sync --locked" in build and "exit 1" in build
    deploy = wf["jobs"]["deploy"]
    assert deploy["permissions"] == {"pages": "write", "id-token": "write"}
    assert any("deploy-pages" in s.get("uses", "") for s in deploy["steps"])
    wiki = wf["jobs"]["wiki"]
    assert wiki["permissions"] == {"contents": "write"}
    script = wiki["steps"][-1]["run"]
    assert "API-Reference.md" in script and "MCP-Tools.md" in script

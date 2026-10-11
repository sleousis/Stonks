"""The Content-Security-Policy of the console and the API.

The console's index.html has small inline scripts (the theme before first
paint, the route preload loop). The policy allows exactly those by their
sha256 hash, so a change to either script must come with a new hash, or
the browser blocks it.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlparse

from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from stonks.api.csp import (
    CONSOLE_SCRIPT_HASHES,
    SecurityHeadersMiddleware,
    console_policy,
    docs_policy,
    inline_script_hashes,
)

ROOT = Path(__file__).parents[2]
INDEX = ROOT / "web" / "src" / "index.html"
CADDYFILE = ROOT / "deploy" / "Caddyfile"


def _directives(policy: str) -> dict[str, list[str]]:
    out = {}
    for part in policy.split(";"):
        words = part.split()
        if words:
            out[words[0]] = words[1:]
    return out


def test_the_policy_allows_every_inline_script_of_index_html_and_nothing_else():
    """Fails when index.html's inline script changes without the hash. Copy
    the hashes this prints into CONSOLE_SCRIPT_HASHES (src/stonks/api/csp.py)."""
    found = inline_script_hashes(INDEX.read_bytes().decode("utf-8"))
    assert found, "index.html has no inline script; drop the hashes"
    assert set(found) == set(CONSOLE_SCRIPT_HASHES), (
        f"index.html inline scripts hash to {found}; update CONSOLE_SCRIPT_HASHES"
    )


def test_index_html_is_checked_out_with_lf_endings():
    """The hash covers the exact bytes, so a CRLF checkout would change it."""
    attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert re.search(r"^web/src/index\.html\s+text eol=lf$", attributes, re.M)
    assert b"\r\n" not in INDEX.read_bytes()


def test_inline_script_hashes_skip_external_and_data_scripts():
    html = (
        "<script>a()</script>"
        '<script src="main.js" type="module"></script>'
        '<script type="application/json" id="d">{"x": 1}</script>'
        "<script data-x>\nb()\n</script>"
    )
    assert inline_script_hashes(html) == [
        "sha256-qVpDBgj7bpq5hMAcGp3AOc79J3Y1Z4HvySTwKrWDoy4=",
        "sha256-NlXKTQe8YfLmZqnEm5aXK1Lb/1wWgkKgNs+lNRY6yOo=",
    ]


def test_the_console_policy_allows_only_this_origin():
    policy = _directives(console_policy())
    assert policy["default-src"] == ["'self'"]
    assert policy["script-src"] == ["'self'", *(f"'{h}'" for h in CONSOLE_SCRIPT_HASHES)]
    # The API, the SSE job streams and web push subscriptions: same origin.
    assert policy["connect-src"] == ["'self'"]
    # Fonts ship with the console (fontsource), never from a font CDN.
    assert policy["font-src"] == ["'self'"]
    assert policy["worker-src"] == ["'self'"]
    assert policy["manifest-src"] == ["'self'"]
    assert policy["object-src"] == ["'none'"]
    assert policy["frame-ancestors"] == ["'none'"]
    assert policy["base-uri"] == ["'self'"]
    assert policy["form-action"] == ["'self'"]
    assert "'unsafe-eval'" not in console_policy()
    assert "'unsafe-inline'" not in policy["script-src"]


def _hosts(policy: str) -> set[str]:
    """Every host a policy names as a source, in any directive."""
    return {
        host
        for sources in _directives(policy).values()
        for src in sources
        if (host := urlparse(src).hostname)
    }


def test_the_docs_policy_allows_the_swagger_cdn_only_there():
    assert {"cdn.jsdelivr.net"} <= _hosts(docs_policy())
    assert _hosts(console_policy()).isdisjoint({"cdn.jsdelivr.net"})
    assert _directives(docs_policy())["frame-ancestors"] == ["'none'"]


def _allowed(sources: list[str], url: str) -> bool:
    origin = "/".join(url.split("/")[:3])
    return any(origin == src.rstrip("/") for src in sources)


def test_the_docs_policy_allows_everything_fastapis_docs_pages_load():
    """Read the URLs from FastAPI's own Swagger UI and ReDoc pages, so a
    FastAPI upgrade that loads something new fails here, not in a browser.
    ReDoc loads the Google Fonts stylesheet, which pulls its fonts from
    fonts.gstatic.com."""
    from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html

    policy = _directives(docs_policy())
    for page in (
        get_redoc_html(openapi_url="/openapi.json", title="t"),
        get_swagger_ui_html(openapi_url="/openapi.json", title="t"),
    ):
        html = bytes(page.body).decode("utf-8")
        loaded = re.findall(r"<(script|link)\b[^>]*?(?:src|href)=\"(https://[^\"]+)\"", html)
        for tag, url in loaded:
            if "favicon" in url:
                directive = "img-src"
            elif tag == "script":
                directive = "script-src"
            else:
                directive = "style-src"
            assert _allowed(policy[directive], url), (directive, url)
        if any(urlparse(url).hostname == "fonts.googleapis.com" for _, url in loaded):
            assert _allowed(policy["font-src"], "https://fonts.gstatic.com/x"), "font-src"


def _client() -> TestClient:
    async def ok(request):
        return PlainTextResponse("ok")

    async def framed(request):
        return PlainTextResponse("ok", headers={"X-Frame-Options": "SAMEORIGIN"})

    app = Starlette(
        routes=[
            Route("/", ok),
            Route("/api/health", ok),
            Route("/docs", ok),
            Route("/redoc", ok),
            Route("/framed", framed),
        ]
    )
    app.add_middleware(SecurityHeadersMiddleware)
    return TestClient(app)


def test_the_middleware_sets_the_console_policy_and_the_basic_headers():
    with _client() as c:
        for path in ("/", "/api/health"):
            headers = c.get(path).headers
            assert headers["content-security-policy"] == console_policy(), path
            assert headers["x-content-type-options"] == "nosniff"
            assert headers["x-frame-options"] == "DENY"
            assert headers["referrer-policy"] == "strict-origin-when-cross-origin"


def test_the_middleware_gives_the_api_docs_their_own_policy():
    with _client() as c:
        for path in ("/docs", "/redoc"):
            assert c.get(path).headers["content-security-policy"] == docs_policy()


def test_the_middleware_keeps_a_header_the_route_set_itself():
    with _client() as c:
        assert c.get("/framed").headers["x-frame-options"] == "SAMEORIGIN"


def _caddy_header(name: str) -> str:
    text = CADDYFILE.read_text(encoding="utf-8")
    match = re.search(rf'^\s*\??{re.escape(name)}\s+"([^"]*)"', text, re.M)
    assert match, f"{name} not set in the Caddyfile"
    return match.group(1)


def test_caddy_sends_the_same_console_policy():
    """Caddy covers every response, its own error pages too (a 502 while the
    api restarts). The app's header wins where it sets one."""
    assert _caddy_header("Content-Security-Policy") == console_policy()
    text = CADDYFILE.read_text(encoding="utf-8")
    assert '?Content-Security-Policy "' in text
